"""工作区快照与 blob store（06-git-integration.md §5）。

V0.1 快照算法（逐文件内容快照 + blob 去重，不做 patch 相减）：

```text
snapshot = {
  head:  当前 HEAD SHA,
  files: { path → content_hash }，仅含相对 HEAD 有差异的文件；
         hash 为 None 表示该路径相对 HEAD 已删除
}
```

文件内容写入按 sha256 去重的 blob store：
``.project-evolution/evidence/snapshots/blob-<sha256>``。

该算法同时解决：Task 开始前的 Dirty Tree 不被算成新变化、
Agent 长时间不 commit、Checkpoint 间增量（对比上一快照而非 Task 起点）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from project_evolution.config import PEL_DIR_NAME
from project_evolution.git.adapter import GitAdapter

_LOG = logging.getLogger(__name__)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WorktreeSnapshot:
    """一次工作区快照的内存表示与持久化。"""

    def __init__(
        self,
        *,
        task_id: str,
        checkpoint_id: str | None,
        head: str | None,
        branch: str | None,
        files: dict[str, str | None],
        created_at: datetime,
    ):
        self.task_id = task_id
        self.checkpoint_id = checkpoint_id
        self.head = head
        self.branch = branch
        self.files = files
        self.created_at = created_at

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "checkpoint_id": self.checkpoint_id,
            "head": self.head,
            "branch": self.branch,
            "files": self.files,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeSnapshot:
        return cls(
            task_id=data["task_id"],
            checkpoint_id=data.get("checkpoint_id"),
            head=data.get("head"),
            branch=data.get("branch"),
            files=data.get("files", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


class BlobStore:
    """内容寻址 blob store：``evidence/snapshots/blob-<sha256>``，写入去重。"""

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def write(self, content: bytes) -> str:
        digest = content_hash(content)
        path = self.path_for(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        return digest

    def read(self, digest: str) -> bytes:
        return self.path_for(digest).read_bytes()

    def path_for(self, digest: str) -> Path:
        return self._dir / f"blob-{digest}"

    @classmethod
    def for_project(cls, project_root: str | Path) -> BlobStore:
        return cls(Path(project_root) / PEL_DIR_NAME / "evidence" / "snapshots")


class SnapshotStore:
    """按 Task 保存快照 JSON：``evidence/snapshots/<task_id>/<label>.json``。"""

    def __init__(self, project_root: str | Path):
        self._root = Path(project_root)
        self._dir = self._root / PEL_DIR_NAME / "evidence" / "snapshots"

    def label_for(self, snapshot: WorktreeSnapshot) -> str:
        return snapshot.checkpoint_id if snapshot.checkpoint_id else "start"

    def save(self, snapshot: WorktreeSnapshot) -> str:
        """保存快照，返回相对项目根目录的 posix 路径（事件中记录该值）。"""
        path = self._dir / snapshot.task_id / f"{self.label_for(snapshot)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return self.relative_path(snapshot.task_id, self.label_for(snapshot))

    def load(self, relative_path: str) -> WorktreeSnapshot | None:
        """加载快照；缺失/损坏返回 None（调用方按 06 §5 标记 scope_confidence=low）。"""
        path = self._root / relative_path
        if not path.is_file():
            _LOG.warning("snapshot file missing: %s", path)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return WorktreeSnapshot.from_dict(data)
        except (OSError, ValueError, KeyError, PydanticValidationError) as exc:
            _LOG.warning("snapshot file unreadable (%s): %s", path, exc)
            return None

    def relative_path(self, task_id: str, label: str) -> str:
        return f"{PEL_DIR_NAME}/evidence/snapshots/{task_id}/{label}.json"


def capture_worktree_snapshot(
    adapter: GitAdapter,
    blobs: BlobStore,
    store: SnapshotStore,
    *,
    task_id: str,
    checkpoint_id: str | None = None,
) -> tuple[WorktreeSnapshot, str]:
    """采集当前工作区快照并持久化，返回 (snapshot, 相对路径)。

    - 仅记录相对 HEAD 有差异的文件（status 已排除 .project-evolution/）；
    - 存在的文件写入 blob store（内容去重），hash 为 None 表示已删除；
    - 目录（submodule 等）跳过，V0.1 不采集其内容。
    """
    files: dict[str, str | None] = {}
    for entry in adapter.get_status_entries():
        worktree_path = adapter.repo_root / entry.path
        if worktree_path.is_dir():
            continue  # submodule/目录：V0.1 不采集
        if worktree_path.is_file():
            files[entry.path] = blobs.write(worktree_path.read_bytes())
        else:
            files[entry.path] = None  # 相对 HEAD 已删除
        if entry.orig_path is not None:
            # 索引中记录的 rename：旧路径相对 HEAD 已不存在
            files[entry.orig_path] = None
    snapshot = WorktreeSnapshot(
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        head=adapter.get_head_sha(),
        branch=adapter.get_branch(),
        files=files,
        created_at=datetime.now(timezone.utc),
    )
    relative = store.save(snapshot)
    return snapshot, relative
