"""ChangeDigestBuilder：发给 Evolution Model 的变更摘要（phase-04 §8/§9）。

输出至少包含：changed_files、added/deleted files、diff_stat、selected_hunks、
tests、summary、truncated flag，并支持大小预算。

Privacy Filtering（06 §8 + phase-04 §9，硬约束）：

- ``.project-evolution/``：一切 digest 永久排除，不可配置关闭
  （防御性再检查；采集层本就不该给出）；
- secret 文件（.env / secret.key / credentials.json / *.pem / *.key 等额外模式）：
  永不进入 digest；
- binary、生成目录（bin/obj/dist/build/node_modules 等）、lock 文件（可配置）：
  不进入 digest，但记录在 excluded 列表中保证可追溯。

Phase 05 的 LLM 调用不在本阶段范围；本模块只做确定性构建。
"""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from project_evolution.git.adapter import is_excluded_path
from project_evolution.git.diff import FileChange, SnapshotIncrement

#: 秘密文件基名（硬编码；privacy.exclude 只能追加，不能关闭）
SECRET_BASENAMES = frozenset(
    {".env", "secret.key", "credentials.json", "id_rsa", "id_ed25519", ".netrc"}
)
#: 秘密文件后缀
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore", ".env")
#: 生成物目录段
GENERATED_SEGMENTS = frozenset(
    {"bin", "obj", "dist", "build", "node_modules", "__pycache__", ".venv", ".git"}
)
#: 优先保留的文件类别（06 §8：source/config/tests/manifest 优先）
SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
        ".c", ".h", ".cpp", ".cc", ".hpp", ".swift", ".rb", ".php", ".vue", ".svelte",
        ".sql", ".sh", ".ps1",
    }
)
CONFIG_SUFFIXES = frozenset(
    {".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".conf", ".xml", ".properties"}
)
TEST_MARKERS = ("test", "spec")


def privacy_exclusion(
    path: str,
    *,
    is_binary: bool = False,
    extra_patterns: tuple[str, ...] = (),
    exclude_locks: bool = True,
) -> str | None:
    """返回排除原因；None 表示允许进入 digest。"""
    normalized = path.replace("\\", "/")
    if is_excluded_path(normalized):
        return "pel-internal"  # 硬编码，不可配置关闭
    name = normalized.rsplit("/", 1)[-1].lower()
    if name in SECRET_BASENAMES or name.endswith(SECRET_SUFFIXES):
        return "secret"
    for pattern in extra_patterns:
        if fnmatch(normalized, pattern) or fnmatch(name, pattern):
            return "secret"
    segments = PurePosixPath(normalized).parts
    if any(segment in GENERATED_SEGMENTS for segment in segments[:-1]):
        return "generated"
    if is_binary:
        return "binary"
    if exclude_locks and name.endswith(".lock"):
        return "lock"
    return None


class SelectedHunk(BaseModel):
    """从 diff 中选出的一个 hunk（大小预算内优先保留）。"""

    path: str
    header: str
    content: str


class DigestEvidence(BaseModel):
    """本 Checkpoint 产生的 Evidence 在 Digest 中的清单条目（05-evolution-analyzer.md §4）。

    模型输出的 ``evidence_ids`` 只能从该清单中选择，Validator 强制执行子集校验；
    清单之外的 Evidence ID 一律视为幻觉，拒绝。
    """

    id: str
    type: str  # git_diff / test_result / raw_event / agent_summary ...
    what: str = ""


class DigestTestResult(BaseModel):
    """Digest 中的测试命令结果（03-event-model.md §3 test_passed/failed）。"""

    command: str
    status: str  # passed / failed
    source: str  # agent_claim / pel_verified
    evidence_id: str | None = None


class DigestAgentSummary(BaseModel):
    """Digest 中的 Agent 事实总结（03-event-model.md §3 agent_summary）。"""

    text: str
    source_agent: str | None = None
    evidence_id: str | None = None


class ChangeDigest(BaseModel):
    """发往 Evolution Model 的确定性变更摘要。"""

    schema_version: int = 1
    project_id: str
    task_id: str
    checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    changed_files: list[str] = Field(default_factory=list)
    added_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[dict[str, Any]] = Field(default_factory=list)
    diff_stat: str = ""
    selected_hunks: list[SelectedHunk] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    summary: str = ""
    truncated: bool = False
    #: 被过滤掉的文件与原因（可追溯，不进入模型输入）
    excluded: list[dict[str, str]] = Field(default_factory=list)
    #: normal / low / degraded（快照缺失或非 Git 时显式降级）
    scope_confidence: str = "normal"
    #: 本 Checkpoint 的全部 Evidence（模型 evidence_ids 的唯一合法来源）
    evidence: list[DigestEvidence] = Field(default_factory=list)
    #: 本 Checkpoint 的测试命令结果（phase-05 §5：带 evidence_id 与 source）
    test_results: list[DigestTestResult] = Field(default_factory=list)
    #: 本 Checkpoint 的 Agent 事实总结（phase-05 §5：带 evidence_id）
    agent_summaries: list[DigestAgentSummary] = Field(default_factory=list)


def _category(path: str) -> int:
    """文件保留优先级：tests > source > config > 其他。"""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0]
    if any(marker in stem for marker in TEST_MARKERS):
        return 0
    if name.endswith(tuple(SOURCE_SUFFIXES)):
        return 1
    if name.endswith(tuple(CONFIG_SUFFIXES)):
        return 2
    return 3


class ChangeDigestBuilder:
    """把 SnapshotIncrement 构建为大小受限的 ChangeDigest。"""

    def __init__(
        self,
        *,
        max_files: int = 50,
        max_total_diff_chars: int = 40_000,
        extra_exclude_patterns: tuple[str, ...] = (),
        exclude_locks: bool = True,
    ):
        self._max_files = max_files
        self._max_chars = max_total_diff_chars
        self._extra = tuple(extra_exclude_patterns)
        self._exclude_locks = exclude_locks

    def build(
        self,
        increment: SnapshotIncrement,
        *,
        project_id: str,
        task_id: str,
        checkpoint_id: str | None = None,
        scope_confidence: str = "normal",
    ) -> ChangeDigest:
        kept: list[FileChange] = []
        excluded: list[dict[str, str]] = []
        for change in increment.changes:
            reason = privacy_exclusion(
                change.path,
                is_binary=change.is_binary,
                extra_patterns=self._extra,
                exclude_locks=self._exclude_locks,
            )
            if reason is None and change.old_path is not None:
                reason = privacy_exclusion(
                    change.old_path,
                    is_binary=change.is_binary,
                    extra_patterns=self._extra,
                    exclude_locks=self._exclude_locks,
                )
            if reason is None:
                kept.append(change)
            else:
                excluded.append({"path": change.path, "reason": reason})

        truncated = False
        if len(kept) > self._max_files:
            kept.sort(key=lambda c: (_category(c.path), c.path))
            kept, dropped = kept[: self._max_files], kept[self._max_files :]
            excluded.extend({"path": c.path, "reason": "file-budget"} for c in dropped)
            truncated = True
        kept.sort(key=lambda c: c.path)

        digest = ChangeDigest(
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            scope_confidence=scope_confidence,
            excluded=excluded,
        )
        for change in kept:
            digest.changed_files.append(change.path)
            if change.change == "added":
                digest.added_files.append(change.path)
            elif change.change == "deleted":
                digest.deleted_files.append(change.path)
            elif change.change == "renamed":
                digest.renamed_files.append(
                    {
                        "old_path": change.old_path,
                        "new_path": change.new_path,
                        "similarity": change.similarity,
                    }
                )
            if _is_test_path(change.path):
                digest.tests.append(change.path)

        insertions = sum(c.added_lines for c in kept)
        deletions = sum(c.deleted_lines for c in kept)
        digest.diff_stat = (
            f"{len(kept)} files changed, {insertions} insertions(+), "
            f"{deletions} deletions(-)"
        )
        digest.selected_hunks, hunks_truncated = self._select_hunks(kept)
        digest.truncated = truncated or hunks_truncated
        digest.summary = self._summary(digest)
        return digest

    # ----------------------------------------------------------------- inside

    def _select_hunks(self, changes: list[FileChange]) -> tuple[list[SelectedHunk], bool]:
        hunks: list[tuple[int, str, str, str]] = []  # (priority, path, header, content)
        for change in changes:
            for header, content in _split_hunks(change.diff_text):
                hunks.append((_category(change.path), change.path, header, content))
        hunks.sort(key=lambda item: (item[0], item[1], item[2]))
        selected: list[SelectedHunk] = []
        used = 0
        truncated = False
        for _priority, path, header, content in hunks:
            size = len(header) + len(content)
            if used + size > self._max_chars:
                truncated = True
                continue  # 整 hunk 粒度：放不下就跳过（低优先级 hunk 可能挤掉部分高优先级）
            selected.append(SelectedHunk(path=path, header=header, content=content))
            used += size
        return selected, truncated

    @staticmethod
    def _summary(digest: ChangeDigest) -> str:
        parts: list[str] = []
        if digest.added_files:
            parts.append("added: " + ", ".join(digest.added_files[:5]))
        if digest.renamed_files:
            parts.append(
                "renamed: "
                + ", ".join(f"{r['old_path']} -> {r['new_path']}" for r in digest.renamed_files[:5])
            )
        modified = [
            p
            for p in digest.changed_files
            if p not in digest.added_files and p not in digest.deleted_files
        ]
        if modified:
            parts.append("modified: " + ", ".join(modified[:5]))
        if digest.deleted_files:
            parts.append("deleted: " + ", ".join(digest.deleted_files[:5]))
        if not parts:
            return "no changes"
        suffix = " ..." if len(digest.changed_files) > 5 else ""
        return digest.diff_stat + " | " + "; ".join(parts) + suffix


def _split_hunks(diff_text: str) -> list[tuple[str, str]]:
    """把 unified diff 文本切成 (header, content) 列表。"""
    hunks: list[tuple[str, str]] = []
    current_header: str | None = None
    current_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            if current_header is not None:
                hunks.append((current_header, "\n".join(current_lines)))
            current_header, current_lines = line, []
        elif current_header is not None:
            current_lines.append(line)
    if current_header is not None:
        hunks.append((current_header, "\n".join(current_lines)))
    return hunks


def _is_test_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0]
    return any(marker in stem for marker in TEST_MARKERS)
