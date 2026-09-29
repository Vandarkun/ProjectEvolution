"""Git 采集端口实现：把 phase-03 的 BaselineRecorder / CheckpointCollector 接到真实 Git。

- :class:`GitBaselineRecorder`：start_task 记录 HEAD/branch/status + 工作区快照；
- :class:`GitCheckpointCollector`：checkpoint 采集快照 → 对比上一快照逐文件增量
  → diff evidence → changed-file RawEvents；
- 非 Git 仓库：:class:`DegradedRecorder` / :class:`DegradedCollector` 明确降级
  （06 §14：无 Git 时仍可工作，但 Evidence 质量降低且必须标明）。

上一快照的解析走 RawEvent Ledger（task_started / checkpoint_created payload 中的
snapshot 路径），因此快照链是 Canonical 可重建的；快照文件缺失时按 06 §5
标记 ``scope_confidence = low``，不伪造精确归属。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from project_evolution.git.adapter import GitAdapter
from project_evolution.git.baseline import (
    BlobStore,
    SnapshotStore,
    capture_worktree_snapshot,
)
from project_evolution.git.diff import FileChange, SnapshotIncrement, compute_increment
from project_evolution.git.evidence import DiffEvidenceStore
from project_evolution.ids import new_id
from project_evolution.models import RawEvent
from project_evolution.tasks.ports import (
    BaselineRecorder,
    CheckpointCollector,
    CheckpointEvidence,
    TaskBaseline,
)
from project_evolution.tasks.store import TaskStore

if TYPE_CHECKING:  # 仅类型检查需要，避免运行时循环导入
    from project_evolution.evolution.change_digest import ChangeDigest
    from project_evolution.tasks import TaskServices

_KIND_FOR_CHANGE = {
    "added": "file_added",
    "modified": "file_modified",
    "deleted": "file_deleted",
    "renamed": "file_modified",  # rename 用 old_path/new_path 表达，便于沿链查询
}


class GitBaselineRecorder:
    """start_task 的 Git baseline 采集（06 §3）。"""

    def __init__(self, adapter: GitAdapter, project_root: str | Path):
        self._adapter = adapter
        self._root = Path(project_root)

    def record_task_baseline(self, task_id: str) -> TaskBaseline:
        blobs = BlobStore.for_project(self._root)
        store = SnapshotStore(self._root)
        snapshot, relative = capture_worktree_snapshot(
            self._adapter, blobs, store, task_id=task_id
        )
        return TaskBaseline(
            head_ref=snapshot.head,
            branch=snapshot.branch,
            status_paths=sorted(p for p, h in snapshot.files.items() if h is not None),
            snapshot_path=relative,
            degraded=False,
        )


class GitCheckpointCollector:
    """checkpoint 的证据采集（06 §4/§5，phase-04 §7）。"""

    def __init__(
        self,
        adapter: GitAdapter,
        project_root: str | Path,
        *,
        project_id: str,
        store: TaskStore,
        max_patch_bytes: int | None = 200_000,
    ):
        self._adapter = adapter
        self._root = Path(project_root)
        self._project_id = project_id
        self._store = store
        self._max_patch_bytes = max_patch_bytes

    def collect_checkpoint_evidence(
        self,
        *,
        task_id: str,
        checkpoint_id: str,
        checkpoint_index: int,
        session_id: str | None,
        source: str,
    ) -> CheckpointEvidence:
        blobs = BlobStore.for_project(self._root)
        snapshots = SnapshotStore(self._root)
        previous = self._load_latest_snapshot(task_id)
        scope_confidence = "normal" if previous is not None else "low"

        snapshot, relative = capture_worktree_snapshot(
            self._adapter, blobs, snapshots, task_id=task_id, checkpoint_id=checkpoint_id
        )
        if previous is not None:
            increment = compute_increment(
                previous, snapshot, adapter=self._adapter, blobs=blobs
            )
        else:
            # 基线快照缺失（中途加入 / 快照被清理）：明确标记歧义，不假装精确
            increment = SnapshotIncrement(base_head=None, head=snapshot.head, changes=[])

        evidence = DiffEvidenceStore(self._root).save(
            evidence_id=new_id("EVD"),
            project_id=self._project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            increment=increment,
            max_patch_bytes=self._max_patch_bytes,
        )
        file_events = [
            _file_event(
                change=change,
                project_id=self._project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                session_id=session_id,
                source=source,
                evidence_id=evidence.id if evidence is not None else None,
            )
            for change in increment.changes
        ]
        return CheckpointEvidence(
            base_ref=previous.head if previous is not None else None,
            head_ref=snapshot.head,
            snapshot_path=relative,
            diff_evidence=evidence,
            file_events=file_events,
            scope_confidence=scope_confidence,
        )

    def _load_latest_snapshot(self, task_id: str):
        """从 Ledger 找该 Task 最近一次记录的快照并加载。"""
        for event in reversed(self._store.replay()):
            if event.task_id != task_id:
                continue
            if event.kind == "checkpoint_created":
                path = event.payload.get("snapshot_path")
            elif event.kind == "task_started":
                path = event.payload.get("baseline_snapshot_path")
            else:
                continue
            if isinstance(path, str) and path:
                return SnapshotStore(self._root).load(path)
        return None


class DegradedRecorder:
    """非 Git 仓库的 baseline 占位：明确 degraded（06 §14）。"""

    def record_task_baseline(self, task_id: str) -> TaskBaseline:
        return TaskBaseline(degraded=True)


class DegradedCollector:
    """非 Git 仓库的 checkpoint 占位：无 Git 证据，明确 degraded。"""

    def collect_checkpoint_evidence(
        self,
        *,
        task_id: str,
        checkpoint_id: str,
        checkpoint_index: int,
        session_id: str | None,
        source: str,
    ) -> CheckpointEvidence:
        return CheckpointEvidence(scope_confidence="degraded")


def create_git_ports(
    project_root: str | Path,
    *,
    project_id: str,
    store: TaskStore,
    max_patch_bytes: int | None = 200_000,
) -> tuple[BaselineRecorder, CheckpointCollector]:
    """探测 Git 仓库并组装采集端口；非 Git 目录返回明确降级实现。"""
    adapter = GitAdapter.detect(project_root)
    if adapter is None:
        return DegradedRecorder(), DegradedCollector()
    return (
        GitBaselineRecorder(adapter, project_root),
        GitCheckpointCollector(
            adapter,
            project_root,
            project_id=project_id,
            store=store,
            max_patch_bytes=max_patch_bytes,
        ),
    )


def create_git_aware_task_services(
    project_root: str | Path, *, project_id: str
) -> TaskServices:
    """便捷工厂：TaskServices + Git 采集端口（non-Git 目录自动降级）。"""
    from project_evolution.tasks import TaskServices

    store = TaskStore(project_root)
    recorder, collector = create_git_ports(
        project_root,
        project_id=project_id,
        store=store,
        max_patch_bytes=store.config.evidence.max_patch_bytes,
    )
    return TaskServices(store, baseline_recorder=recorder, checkpoint_collector=collector)


def build_digest_for_checkpoint(
    project_root: str | Path,
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
) -> ChangeDigest:
    """从 Ledger 重建某 checkpoint 的 ChangeDigest（Phase 05 的模型输入）。

    读取该 checkpoint 事件引用的快照与其上一份快照，重新计算增量后构建 digest；
    快照缺失时 scope_confidence=low，non-Git 目录返回 degraded 空 digest。
    """
    from project_evolution.evolution.change_digest import ChangeDigestBuilder
    from project_evolution.tasks import TaskStore

    root = Path(project_root)
    adapter = GitAdapter.detect(root)
    store = TaskStore(root)
    snapshots = SnapshotStore(root)

    current_path: str | None = None
    previous_path: str | None = None
    for event in store.replay():
        if event.task_id != task_id:
            continue
        if event.kind == "checkpoint_created" and event.checkpoint_id == checkpoint_id:
            current_path = event.payload.get("snapshot_path")
            break
        if event.kind == "checkpoint_created":
            path = event.payload.get("snapshot_path")
        elif event.kind == "task_started":
            path = event.payload.get("baseline_snapshot_path")
        else:
            continue
        if isinstance(path, str) and path:
            previous_path = path

    builder = ChangeDigestBuilder(
        max_files=store.config.change_digest.max_files,
        max_total_diff_chars=store.config.change_digest.max_total_diff_chars,
        extra_exclude_patterns=tuple(store.config.privacy.exclude),
    )
    if adapter is None or current_path is None:
        return builder.build(
            SnapshotIncrement(),
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            scope_confidence="degraded" if adapter is None else "low",
        )
    current = snapshots.load(current_path)
    previous = snapshots.load(previous_path) if previous_path else None
    if current is None or previous is None:
        return builder.build(
            SnapshotIncrement(head=current.head if current else None),
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            scope_confidence="low",
        )
    increment = compute_increment(
        previous, current, adapter=adapter, blobs=BlobStore.for_project(root)
    )
    return builder.build(
        increment,
        project_id=project_id,
        task_id=task_id,
        checkpoint_id=checkpoint_id,
    )


def _file_event(
    *,
    change: FileChange,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    session_id: str | None,
    source: str,
    evidence_id: str | None,
) -> RawEvent:
    payload: dict = {
        "path": change.path,
        "content_hash": change.new_hash or change.old_hash,
    }
    if change.change == "renamed":
        payload["old_path"] = change.old_path
        payload["new_path"] = change.new_path
        payload["change"] = "renamed"
        payload["similarity"] = change.similarity
    if evidence_id is not None:
        payload["evidence_id"] = evidence_id
    return RawEvent(
        id=new_id("RAW"),
        project_id=project_id,
        timestamp=datetime.now(timezone.utc),
        kind=_KIND_FOR_CHANGE[change.change],
        source=source,
        task_id=task_id,
        session_id=session_id,
        checkpoint_id=checkpoint_id,
        payload=payload,
    )
