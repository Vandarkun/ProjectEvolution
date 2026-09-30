"""Task 体系：Task / Session / Checkpoint 生命周期服务（phase-03）。

Canonical 事实源是 RawEvent Ledger；runtime journal 只是恢复用镜像。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from project_evolution.tasks.checkpoint_service import CheckpointService
from project_evolution.tasks.journal import TaskJournal
from project_evolution.tasks.ports import (
    BaselineRecorder,
    CheckpointCollector,
    CheckpointEvidence,
    TaskBaseline,
)
from project_evolution.tasks.session_service import SessionService
from project_evolution.tasks.state import (
    SessionRuntimeState,
    TaskRuntimeState,
    project_session_states,
    project_task_session_links,
    project_task_states,
)
from project_evolution.tasks.store import TaskStore
from project_evolution.tasks.task_service import TaskService

__all__ = [
    "BaselineRecorder",
    "CheckpointCollector",
    "CheckpointEvidence",
    "CheckpointService",
    "SessionRuntimeState",
    "SessionService",
    "TaskBaseline",
    "TaskJournal",
    "TaskRuntimeState",
    "TaskService",
    "TaskStore",
    "create_task_services",
    "project_session_states",
    "project_task_session_links",
    "project_task_states",
]


class TaskServices:
    """同一 TaskStore 上的三个服务集合，方便调用方共享投影。

    ``analysis_worker``：phase-05 起组合根可注入的后台语义分析 worker
    （仅当传入 analysis_provider 时存在）。
    """

    analysis_worker: Any | None = None

    def __init__(
        self,
        store: TaskStore,
        *,
        baseline_recorder: BaselineRecorder | None = None,
        checkpoint_collector: CheckpointCollector | None = None,
        on_checkpoint: Callable[[str, str], None] | None = None,
    ):
        self.store = store
        self.tasks = TaskService(store, baseline_recorder=baseline_recorder)
        self.sessions = SessionService(store)
        self.checkpoints = CheckpointService(
            store, collector=checkpoint_collector, on_checkpoint=on_checkpoint
        )


def create_task_services(
    project_root: str | Path,
    *,
    baseline_recorder: BaselineRecorder | None = None,
    checkpoint_collector: CheckpointCollector | None = None,
) -> TaskServices:
    """便捷工厂：Phase 04 注入 Git 采集端口，Phase 03 使用默认占位。"""
    return TaskServices(
        TaskStore(project_root),
        baseline_recorder=baseline_recorder,
        checkpoint_collector=checkpoint_collector,
    )
