"""Diff Evidence 持久化（06-git-integration.md §7、04-event-ledger.md §8）。

Checkpoint 的增量 patch 保存到 ``.project-evolution/evidence/diffs/<EVD-id>.patch``，
同时返回 Evidence 记录（ref = patch 内容 sha256，path = 相对项目根路径）。

隐私与体积约束：

- secret 命中的文件只记录 path + hash，绝不把内容写进 patch；
- binary 文件只标记变化，不落内容；
- patch 超过字节预算时按文件段落边界裁剪并标记 truncated。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from project_evolution.config import PEL_DIR_NAME
from project_evolution.git.diff import FileChange, SnapshotIncrement
from project_evolution.models import Evidence

#: 秘密文件：只记 path/hash，内容永不落盘（保守默认，可被 privacy.exclude 追加）
_SECRET_BASENAMES = frozenset(
    {".env", "secret.key", "credentials.json", "id_rsa", "id_ed25519", ".netrc"}
)
_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore", ".env")


def is_secret_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _SECRET_BASENAMES:
        return True
    return name.endswith(_SECRET_SUFFIXES)


class DiffEvidenceStore:
    """保存 checkpoint 增量 patch 并生成 Evidence 记录。"""

    def __init__(self, project_root: str | Path):
        self._root = Path(project_root)
        self._diffs_dir = self._root / PEL_DIR_NAME / "evidence" / "diffs"

    def diffs_dir(self) -> Path:
        return self._diffs_dir

    def save(
        self,
        *,
        evidence_id: str,
        project_id: str,
        task_id: str,
        checkpoint_id: str,
        increment: SnapshotIncrement,
        max_patch_bytes: int | None = None,
    ) -> Evidence | None:
        """写入 patch 文件并返回 Evidence；无任何变化时返回 None（不落空文件）。"""
        sections = [self._section(change) for change in increment.changes]
        sections = [section for section in sections if section]
        if not sections:
            return None

        header = "\n".join(
            [
                f"# PEL diff evidence {evidence_id}",
                f"# project: {project_id} task: {task_id} checkpoint: {checkpoint_id}",
                f"# base_ref: {increment.base_head or 'none'}",
                f"# head_ref: {increment.head or 'none'}",
            ]
        )
        truncated = False
        body = "\n".join(sections)
        if max_patch_bytes is not None and len(body.encode("utf-8")) > max_patch_bytes:
            body = self._truncate(sections, max_patch_bytes)
            truncated = True
        text = (
            header
            + f"\n# truncated: {str(truncated).lower()}\n\n"
            + body
            + ("\n" if not body.endswith("\n") else "")
        )

        self._diffs_dir.mkdir(parents=True, exist_ok=True)
        path = self._diffs_dir / f"{evidence_id}.patch"
        tmp = path.with_name(path.name + ".tmp")
        # newline="" 保证文件字节与 sha256 计算基准一致（Windows 不做 \n -> \r\n 转换）
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(path)

        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return Evidence(
            id=evidence_id,
            project_id=project_id,
            type="git_diff",
            ref=sha256,
            path=path.relative_to(self._root).as_posix(),
            created_at=datetime.now(timezone.utc),
            metadata={
                "task_id": task_id,
                "checkpoint_id": checkpoint_id,
                "base_ref": increment.base_head,
                "head_ref": increment.head,
                "sha256": sha256,
                "truncated": truncated,
                "files": [change.path for change in increment.changes],
            },
        )

    # ----------------------------------------------------------------- inside

    @staticmethod
    def _section(change: FileChange) -> str:
        title = change.path if change.change != "renamed" else (
            f"{change.old_path} -> {change.path}"
        )
        if is_secret_path(change.path) or (
            change.old_path is not None and is_secret_path(change.old_path)
        ):
            return (
                f"--- {title} changed [{change.change}]\n"
                f"+++ content withheld: secret-looking path "
                f"(hash {change.old_hash} -> {change.new_hash})"
            )
        if change.is_binary:
            return (
                f"--- {title} changed [{change.change}]\n"
                f"+++ binary file changed (hash {change.old_hash} -> {change.new_hash})"
            )
        if not change.diff_text:
            return ""  # 内容未变（纯 rename）不产生 diff 段
        return change.diff_text.rstrip("\n")

    @staticmethod
    def _truncate(sections: list[str], max_bytes: int) -> str:
        kept: list[str] = []
        used = 0
        for section in sections:
            size = len(section.encode("utf-8")) + 1
            if used + size > max_bytes:
                break
            kept.append(section)
            used += size
        dropped = len(sections) - len(kept)
        text = "\n".join(kept)
        if dropped:
            text += f"\n# ... truncated: {dropped} file section(s) omitted (size budget)"
        return text
