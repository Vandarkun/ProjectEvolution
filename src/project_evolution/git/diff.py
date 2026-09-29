"""快照增量计算（06-git-integration.md §5）。

Checkpoint N 的增量 = 上一份快照（start_task 或 Checkpoint N-1）→ 当前快照：

```text
对每个 path：
  old = path 在上一快照 files 中 → blob 内容
      = 不在、但存在于上一快照的 head → git show <head>:path
      = 都不存在 → 新增文件
  new = 当前 blob 内容；文件已删除则为空
```

不存在"两个 patch 相减"的标准 Git 操作，本模块不做补丁代数，
只做逐文件内容对比 + `git diff --no-index`。

rename 识别（06 §13）：delete + add 且内容 hash 相同（similarity=1.0）
或内容相近（difflib ratio ≥ 阈值）配对；两快照 HEAD 不同步时用
`git diff --find-renames` 交叉验证并记录为 rename_hints（信息性）。
"""

from __future__ import annotations

import difflib
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from project_evolution.git.adapter import GitAdapter
from project_evolution.git.baseline import BlobStore, WorktreeSnapshot, content_hash

#: 内容相近判定阈值：rename+edit 场景
RENAME_SIMILARITY_THRESHOLD = 0.6
#: 参与相似度判定的单文件大小上限（字节）
_SIMILARITY_MAX_BYTES = 512 * 1024
#: 相似度配对的最小内容长度：过小的文件差异不足以区分 rename 与删+增，不配对
_SIMILARITY_MIN_BYTES = 128
#: 二进制嗅探窗口
_SNIFF_WINDOW = 8192

ChangeKind = Literal["added", "modified", "deleted", "renamed"]


class FileChange(BaseModel):
    """快照增量中的单个文件变化。"""

    path: str
    change: ChangeKind
    old_path: str | None = None
    new_path: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    similarity: float | None = None
    is_binary: bool = False
    #: 规范化后的 unified diff 文本（binary 或无差异时为空）
    diff_text: str = ""
    added_lines: int = 0
    deleted_lines: int = 0


class SnapshotIncrement(BaseModel):
    """两份快照之间的增量。"""

    schema_version: int = 1
    base_head: str | None = None
    head: str | None = None
    changes: list[FileChange] = Field(default_factory=list)
    #: git diff --find-renames 的交叉验证结果（new_path -> old_path，信息性）
    rename_hints: dict[str, str] = Field(default_factory=dict)


class _DiffContext:
    """增量计算所需的只读依赖（adapter + blob store）。"""

    def __init__(self, adapter: GitAdapter, blobs: BlobStore):
        self.adapter = adapter
        self.blobs = blobs

    def blob_content(self, digest: str | None) -> bytes | None:
        if digest is None:
            return None
        return self.blobs.read(digest)


def compute_increment(
    old: WorktreeSnapshot, new: WorktreeSnapshot, *, adapter: GitAdapter, blobs: BlobStore
) -> SnapshotIncrement:
    """对比两份快照，逐文件计算增量 diff 并识别 rename。"""
    ctx = _DiffContext(adapter, blobs)
    changes: list[FileChange] = []
    for path in sorted(set(old.files) | set(new.files)):
        in_old, in_new = path in old.files, path in new.files
        old_hash = old.files.get(path)
        new_hash = new.files.get(path)
        if in_old and in_new and old_hash == new_hash:
            continue  # 两快照间未变化
        old_content = _content_at(old, path, ctx)
        new_content = _content_at(new, path, ctx)
        if old_content == new_content:
            continue  # 内容一致（理论上不会走到，防御性保留）
        if old_content is None:
            changes.append(
                _build_change(path, "added", None, new_content, ctx)
            )
        elif new_content is None:
            changes.append(
                _build_change(path, "deleted", old_content, None, ctx)
            )
        else:
            changes.append(
                _build_change(path, "modified", old_content, new_content, ctx)
            )

    changes = _detect_renames(changes, ctx)

    rename_hints: dict[str, str] = {}
    if old.head and new.head and old.head != new.head:
        try:
            rename_hints = adapter.find_renames_between(old.head, new.head)
        except Exception:  # noqa: BLE001 - 交叉验证失败不影响主结果
            rename_hints = {}

    return SnapshotIncrement(
        base_head=old.head,
        head=new.head,
        changes=changes,
        rename_hints=rename_hints,
    )


# --------------------------------------------------------------------- inside


def _content_at(
    snapshot: WorktreeSnapshot, path: str, ctx: _DiffContext
) -> bytes | None:
    """解析某快照时刻 path 的内容；None 表示该时刻文件不存在。

    - 在 files 中：读 blob（hash 为 None 表示已删除）；
    - 不在 files 中：与该快照的 HEAD 一致，从 git 对象读取。
    """
    if path in snapshot.files:
        digest = snapshot.files[path]
        return ctx.blob_content(digest)
    if snapshot.head is None:
        return None
    if not ctx.adapter.file_exists_in_rev(snapshot.head, path):
        return None
    return ctx.adapter.show_file(snapshot.head, path)


def _looks_binary(data: bytes) -> bool:
    return b"\0" in data[:_SNIFF_WINDOW]


def _build_change(
    path: str,
    change: ChangeKind,
    old_content: bytes | None,
    new_content: bytes | None,
    ctx: _DiffContext,
    *,
    old_hash: str | None = None,
    similarity: float | None = None,
    old_path: str | None = None,
) -> FileChange:
    old_bytes = old_content if old_content is not None else b""
    new_bytes = new_content if new_content is not None else b""
    # 两侧内容统一落入 blob store（去重）：old 侧可能来自 git 对象（git show），
    # 不落盘的话后续 rename 配对 / digest 按 hash 取内容会失败
    if old_content is not None:
        ctx.blobs.write(old_bytes)
    if new_content is not None:
        ctx.blobs.write(new_bytes)
    is_binary = _looks_binary(old_bytes) or _looks_binary(new_bytes)
    diff_text = "" if is_binary else _unified_diff(path, old_bytes, new_bytes, ctx)
    added, deleted = _count_lines(diff_text)
    return FileChange(
        path=path,
        change=change,
        old_path=old_path,
        new_path=path if change == "renamed" else None,
        old_hash=old_hash
        if old_hash is not None
        else (content_hash(old_content) if old_content is not None else None),
        new_hash=content_hash(new_content) if new_content is not None else None,
        similarity=similarity,
        is_binary=is_binary,
        diff_text=diff_text,
        added_lines=added,
        deleted_lines=deleted,
    )


def _unified_diff(
    rel_path: str, old_bytes: bytes, new_bytes: bytes, ctx: _DiffContext
) -> str:
    """`git diff --no-index` 生成 unified diff，并把临时路径规范化为 a/<path> b/<path>。"""
    if old_bytes == new_bytes:
        return ""
    with tempfile.TemporaryDirectory(prefix="pel-diff-") as td:
        base = Path(td)
        old_file = base / "old" / rel_path
        new_file = base / "new" / rel_path
        old_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(old_bytes)
        new_file.write_bytes(new_bytes)
        text = ctx.adapter.diff_no_index(
            old_file.relative_to(base).as_posix(),
            new_file.relative_to(base).as_posix(),
            cwd=base,
        )
    text = text.replace(f"a/old/{rel_path}", f"a/{rel_path}")
    text = text.replace(f"b/new/{rel_path}", f"b/{rel_path}")
    # 空内容侧（新增/删除文件）的对比对象是空文件，改写为 /dev/null 语义便于阅读
    if not old_bytes:
        text = text.replace(f"--- a/{rel_path}", "--- /dev/null", 1)
    if not new_bytes:
        text = text.replace(f"+++ b/{rel_path}", "+++ /dev/null", 1)
    return text


def _count_lines(diff_text: str) -> tuple[int, int]:
    added = deleted = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted


def _detect_renames(changes: list[FileChange], ctx: _DiffContext) -> list[FileChange]:
    """把 delete+add 配对成 rename：先精确 hash，再内容相似度。"""
    added = [c for c in changes if c.change == "added"]
    deleted = [c for c in changes if c.change == "deleted"]
    others = [c for c in changes if c.change not in ("added", "deleted")]
    results: list[FileChange] = list(others)
    used_deleted: set[int] = set()
    deleted_contents = {i: ctx.blob_content(c.old_hash) for i, c in enumerate(deleted)}

    for add in added:
        add_content = ctx.blob_content(add.new_hash)
        pair_index = None
        similarity: float | None = None
        for i, old in enumerate(deleted):
            if i in used_deleted or old.is_binary or add.is_binary:
                continue
            if add.new_hash is not None and add.new_hash == old.old_hash:
                pair_index, similarity = i, 1.0
                break
        if pair_index is None:
            for i, old in enumerate(deleted):
                if i in used_deleted or old.is_binary or add.is_binary:
                    continue
                if Path(add.path).suffix != Path(old.path).suffix:
                    continue
                old_bytes, new_bytes = deleted_contents[i], add_content
                if old_bytes is None or new_bytes is None:
                    continue
                if (
                    len(old_bytes) < _SIMILARITY_MIN_BYTES
                    or len(new_bytes) < _SIMILARITY_MIN_BYTES
                ):
                    continue
                if len(old_bytes) > _SIMILARITY_MAX_BYTES or len(new_bytes) > _SIMILARITY_MAX_BYTES:
                    continue
                ratio = difflib.SequenceMatcher(
                    None,
                    old_bytes.decode("utf-8", "replace"),
                    new_bytes.decode("utf-8", "replace"),
                ).ratio()
                if ratio >= RENAME_SIMILARITY_THRESHOLD:
                    pair_index, similarity = i, ratio
                    break
        if pair_index is None:
            results.append(add)
            continue
        old = deleted[pair_index]
        used_deleted.add(pair_index)
        results.append(
            _build_change(
                add.path,
                "renamed",
                deleted_contents[pair_index],
                add_content,
                ctx,
                old_hash=old.old_hash,
                similarity=round(similarity, 4) if similarity is not None else None,
                old_path=old.path,
            )
        )
    for i, old in enumerate(deleted):
        if i not in used_deleted:
            results.append(old)
    return sorted(results, key=lambda c: c.path)
