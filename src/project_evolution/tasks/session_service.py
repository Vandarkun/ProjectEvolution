"""SessionService：Session 生命周期与 Task-Session 绑定。

Session 表示一次 Coding Agent 会话，不等于 Task（02-data-model.md §4）。
一个 Task 可以跨多个 Session（phase-03 §8）：
Session 结束不会自动完成 Task，后续 Session 通过 bind_task 接续。
"""

from __future__ import annotations

from datetime import datetime, timezone

from project_evolution.errors import (
    InvalidStateError,
    SessionNotFoundError,
    TaskNotFoundError,
    ValidationError,
)
from project_evolution.ids import new_id
from project_evolution.models import RawEvent, Session, TaskSessionLink
from project_evolution.tasks.state import SessionRuntimeState, TaskRuntimeState
from project_evolution.tasks.store import TaskStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(
    store: TaskStore,
    project_id: str,
    agent: str,
    external_session_id: str | None = None,
    *,
    source: str = "cli",
    session_id: str | None = None,
) -> Session:
    """创建 Session 并写入 session_started 事件。

    必须已在 store 写锁内调用（TaskService 复用此入口，避免服务间循环依赖）。
    """
    if not project_id:
        raise ValidationError("project_id must be a non-empty string")
    if not agent:
        raise ValidationError("agent must be a non-empty string")
    sid = session_id if session_id is not None else new_id("SES")
    event = RawEvent(
        id=new_id("RAW"),
        project_id=project_id,
        timestamp=_now(),
        kind="session_started",
        source=source,
        session_id=sid,
        payload={
            "agent": agent,
            "external_session_id": external_session_id,
        },
    )
    store.append_assuming_locked([event])
    return Session(
        id=sid,
        project_id=project_id,
        agent=agent,
        started_at=event.timestamp,
        external_session_id=external_session_id,
    )


class SessionService:
    def __init__(self, store: TaskStore):
        self._store = store

    # ------------------------------------------------------------------ read

    def get_session(self, session_id: str) -> Session | None:
        state = self._store.project_session(session_id)
        return state.to_session() if state is not None else None

    def list_sessions(self) -> list[Session]:
        states = self._store.project_sessions().values()
        return [state.to_session() for state in sorted(states, key=lambda s: s.started_at)]

    # ----------------------------------------------------------------- write

    def start_session(
        self,
        project_id: str,
        agent: str,
        external_session_id: str | None = None,
        *,
        source: str = "cli",
    ) -> Session:
        with self._store.locked():
            return create_session(
                self._store, project_id, agent, external_session_id, source=source
            )

    def end_session(
        self, session_id: str, reason: str | None = None, *, source: str = "cli"
    ) -> Session:
        """结束 Session。重复 end 幂等返回，不产生新事件。"""
        with self._store.locked():
            state = self._store.project_session(session_id)
            if state is None:
                raise SessionNotFoundError(f"session not found: {session_id}")
            if state.ended_at is not None:
                return state.to_session()  # 幂等：终态重复调用不追加事件
            event = RawEvent(
                id=new_id("RAW"),
                project_id=state.project_id,
                timestamp=_now(),
                kind="session_ended",
                source=source,
                session_id=session_id,
                payload={"reason": reason},
            )
            self._store.append_assuming_locked([event])
            state.ended_at = event.timestamp
            return state.to_session()

    def bind_task(self, task_id: str, session_id: str, *, source: str = "cli") -> TaskSessionLink:
        """把 Session 绑定到 Task（Session Continuation 的入口）。

        - 幂等：同一 session 已 open 绑定到同一 task 时直接返回现有 link；
        - 冲突：session 已 open 绑定到另一个非终态 task 时拒绝。
        """
        with self._store.locked():
            task = self._require_task(task_id)
            session = self._require_session(session_id)
            self._validate_binding(task, session)
            existing = self._find_open_link(task_id, session_id)
            if existing is not None:
                return existing  # 幂等
            self._reject_conflicting_open_link(session_id, task_id)
            event = RawEvent(
                id=new_id("RAW"),
                project_id=task.project_id,
                timestamp=_now(),
                kind="task_session_bound",
                source=source,
                task_id=task_id,
                session_id=session_id,
                payload={"agent": session.agent},
            )
            self._store.append_assuming_locked([event])
            link = self._find_open_link(task_id, session_id)
            assert link is not None
            return link

    # ----------------------------------------------------------------- inside

    def _require_task(self, task_id: str) -> TaskRuntimeState:
        task = self._store.project_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    def _require_session(self, session_id: str) -> SessionRuntimeState:
        session = self._store.project_session(session_id)
        if session is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return session

    @staticmethod
    def _validate_binding(task: TaskRuntimeState, session: SessionRuntimeState) -> None:
        if task.project_id != session.project_id:
            raise ValidationError(
                f"task {task.task_id} and session {session.session_id} belong to "
                f"different projects"
            )
        if task.status in ("completed", "cancelled"):
            raise InvalidStateError(
                f"cannot bind session {session.session_id} to terminal task {task.task_id} "
                f"(status={task.status})"
            )
        if session.ended_at is not None:
            raise InvalidStateError(
                f"cannot bind ended session {session.session_id} to task {task.task_id}"
            )

    def _find_open_link(self, task_id: str, session_id: str) -> TaskSessionLink | None:
        for link in self._store.project_links():
            if (
                link.task_id == task_id
                and link.session_id == session_id
                and link.left_at is None
            ):
                return link
        return None

    def _reject_conflicting_open_link(self, session_id: str, excluding_task_id: str) -> None:
        tasks = self._store.project_tasks()
        for link in self._store.project_links():
            if link.session_id != session_id or link.left_at is not None:
                continue
            if link.task_id == excluding_task_id:
                continue
            other = tasks.get(link.task_id)
            if other is not None and other.status not in ("completed", "cancelled"):
                raise InvalidStateError(
                    f"session {session_id} is already bound to active task "
                    f"{link.task_id} (status={other.status}); end it or bind after completion"
                )
