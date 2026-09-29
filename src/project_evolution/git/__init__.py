"""Git 集成层：只读 adapter、工作区快照、增量 diff 与 Evidence（phase-04）。

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
from project_evolution.git.collector import (
    DegradedCollector,
    DegradedRecorder,
    GitBaselineRecorder,
    GitCheckpointCollector,
    build_digest_for_checkpoint,
    create_git_aware_task_services,
    create_git_ports,
)
from project_evolution.git.diff import (
    FileChange,
    SnapshotIncrement,
    compute_increment,
)
from project_evolution.git.evidence import DiffEvidenceStore, is_secret_path

__all__ = [
    "BlobStore",
    "CommitMetadata",
    "DegradedCollector",
    "DegradedRecorder",
    "DiffEvidenceStore",
    "FileChange",
    "GitAdapter",
    "GitBaselineRecorder",
    "GitCheckpointCollector",
    "GitError",
    "GitSafetyError",
    "SnapshotIncrement",
    "SnapshotStore",
    "StatusEntry",
    "WorktreeSnapshot",
    "build_digest_for_checkpoint",
    "capture_worktree_snapshot",
    "compute_increment",
    "content_hash",
    "create_git_aware_task_services",
    "create_git_ports",
    "is_excluded_path",
    "is_secret_path",
    "normalize_path",
]
