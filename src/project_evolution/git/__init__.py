"""Git 集成层：只读 adapter、工作区快照与增量 diff（phase-04）。

本层是只读 Evidence Adapter（06 §15）：禁止 add/commit/checkout/reset/clean/
merge/tag；所有命令参数数组 + 超时 + 固定 repo root。
"""

from project_evolution.git.adapter import (
    CommitMetadata,
    GitAdapter,
    GitError,
    GitSafetyError,
    StatusEntry,
    is_excluded_path,
    normalize_path,
)
from project_evolution.git.baseline import (
    BlobStore,
    SnapshotStore,
    WorktreeSnapshot,
    capture_worktree_snapshot,
    content_hash,
)
from project_evolution.git.diff import (
    FileChange,
    SnapshotIncrement,
    compute_increment,
)

__all__ = [
    "BlobStore",
    "CommitMetadata",
    "FileChange",
    "GitAdapter",
    "GitError",
    "GitSafetyError",
    "SnapshotIncrement",
    "SnapshotStore",
    "StatusEntry",
    "WorktreeSnapshot",
    "capture_worktree_snapshot",
    "compute_increment",
    "content_hash",
    "is_excluded_path",
    "normalize_path",
]
