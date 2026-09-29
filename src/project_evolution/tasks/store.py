"""Task 体系的共享组合根：Ledger + 配置 + Journal + 投影。

三个服务（Task / Session / Checkpoint）共享同一个 :class:`TaskStore`，
保证它们看到同一个账本与同一份投影。服务层的关键不变式是
"read-project-then-append" 必须整体在项目写锁内完成（避免两个 writer
算出相同的 checkpoint index），因此提供 :meth:`locked` 上下文，
配合 :meth:`append_assuming_locked` 使用（写锁不可重入，见 ledger/lock.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.ledger import RawEventLedger
from project_evolution.ledger.lock import write_lock
from project_evolution.models import RawEvent, TaskSessionLink
from project_evolution.tasks.journal import TaskJournal
from project_evolution.tasks.state import (
    SessionRuntimeState,
    TaskRuntimeState,
    project_session_states,
    project_task_session_links,
    project_task_states,
)

_LOG = logging.getLogger(__name__)


class TaskStore:
    """单项目的 Task 领域存储（Ledger 为唯一事实源）。"""

    def __init__(self, project_root: str | Path, *, config: PelConfig | None = None):
        self.root = Path(project_root)
        self.config = config if config is not None else load_config(self.root)
        pel_dir = self.root / PEL_DIR_NAME
        self.ledger = RawEventLedger(
            self.root,
            events_path=pel_dir / self.config.events_dir / "raw.jsonl",
            lock_path=pel_dir / self.config.runtime_dir / "locks" / "ledger.lock",
        )
        self.journal = TaskJournal(pel_dir / self.config.runtime_dir / "tasks")

    # ------------------------------------------------------------------ locks

    @contextmanager
    def locked(self) -> Iterator[None]:
        """项目级写锁临界区；内部追加必须用 append_assuming_locked。"""
        with write_lock(self.ledger.lock_path):
            yield

    # ------------------------------------------------------------------ write

    def append_assuming_locked(self, events: list[RawEvent]) -> list[RawEvent]:
        """在已持有项目写锁时追加事件（见 locked()）。"""
        return self.ledger.append_many(events, assume_locked=True)

    def append(self, events: list[RawEvent]) -> list[RawEvent]:
        """独立追加（自带锁）；服务层的复合操作应使用 locked() + 本方法的变体。"""
        return self.ledger.append_many(events)

    # ------------------------------------------------------------------- read

    def replay(self) -> list[RawEvent]:
        """按 seq 顺序重放全部事件（读取端排序唯一依据是 seq）。"""
        return list(self.ledger.iter_events())

    def project_tasks(self) -> dict[str, TaskRuntimeState]:
        return project_task_states(self.replay())

    def project_task(self, task_id: str) -> TaskRuntimeState | None:
        return self.project_tasks().get(task_id)

    def project_sessions(self) -> dict[str, SessionRuntimeState]:
        return project_session_states(self.replay())

    def project_session(self, session_id: str) -> SessionRuntimeState | None:
        return self.project_sessions().get(session_id)

    def project_links(self) -> list[TaskSessionLink]:
        return project_task_session_links(self.replay())
