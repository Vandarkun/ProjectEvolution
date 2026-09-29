"""Task / Session / TaskSessionLink 的 Ledger 投影。

Canonical 事实只存在于 RawEvent Ledger（phase-03 §9：Runtime State 不是唯一事实源）；
本模块把 seq 有序的事件流重放成方便服务层使用的运行时状态。

投影时执行与写入侧相同的状态机校验（02-data-model.md §5 固定转换表）：
账本中出现非法序列（重复 start、终态后生命周期事件、checkpoint index 回退等）
视为写入侧被绕过或数据损坏，立即抛 :class:`InvalidStateError`，绝不静默吞掉。

phase-03 引入的 RawEvent kind（03-event-model.md 未定义，属本阶段采集层扩展，
payload 约束见 models/events.py）：

- ``session_started``     payload: agent, external_session_id?
- ``session_ended``       payload: reason?
- ``task_session_bound``  payload: agent
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from project_evolution.errors import InvalidStateError
from project_evolution.models import RawEvent, Session, Task, TaskSessionLink, TaskStatus
from project_evolution.models.task import check_task_transition

_LIFECYCLE_TARGET: dict[str, TaskStatus] = {
    "task_paused": "paused",
    "task_resumed": "active",
    "task_completed": "completed",
    "task_cancelled": "cancelled",
}


class TaskRuntimeState(BaseModel):
    """从 Ledger 重放得到的 Task 当前状态（投影，非 Canonical）。"""

    schema_version: int = 1
    task_id: str
    project_id: str
    title: str
    status: TaskStatus
    started_at: datetime
    finished_at: datetime | None = None
    baseline_ref: str | None = None
    final_ref: str | None = None
    checkpoint_count: int = 0
    next_checkpoint_index: int = 1
    session_ids: list[str] = Field(default_factory=list)
    last_seq: int | None = None
    updated_at: datetime

    def to_task(self) -> Task:
        return Task(
            id=self.task_id,
            project_id=self.project_id,
            title=self.title,
            status=self.status,
            started_at=self.started_at,
            finished_at=self.finished_at,
            baseline_ref=self.baseline_ref,
        )


class SessionRuntimeState(BaseModel):
    """从 Ledger 重放得到的 Session 当前状态（投影，非 Canonical）。"""

    schema_version: int = 1
    session_id: str
    project_id: str
    agent: str
    started_at: datetime
    ended_at: datetime | None = None
    external_session_id: str | None = None
    bound_task_ids: list[str] = Field(default_factory=list)
    last_seq: int | None = None
    updated_at: datetime

    def to_session(self) -> Session:
        return Session(
            id=self.session_id,
            project_id=self.project_id,
            agent=self.agent,
            started_at=self.started_at,
            ended_at=self.ended_at,
            external_session_id=self.external_session_id,
        )


def _require_task(
    states: dict[str, TaskRuntimeState], event: RawEvent, action: str
) -> TaskRuntimeState:
    task_id = event.task_id
    if task_id is None or task_id not in states:
        raise InvalidStateError(
            f"{action} event {event.id} (seq={event.seq}) references unknown task {task_id!r}"
        )
    return states[task_id]


def project_task_states(events: Iterable[RawEvent]) -> dict[str, TaskRuntimeState]:
    """把 seq 有序事件流重放为 task_id -> TaskRuntimeState。

    :raises InvalidStateError: 事件序列违反状态机或 checkpoint 单调递增。
    """
    states: dict[str, TaskRuntimeState] = {}
    for event in events:
        kind = event.kind
        if kind == "task_started":
            task_id = event.task_id
            if task_id is None:
                raise InvalidStateError(f"task_started event {event.id} has no task_id")
            if task_id in states:
                raise InvalidStateError(f"duplicate task_started for task {task_id}")
            states[task_id] = TaskRuntimeState(
                task_id=task_id,
                project_id=event.project_id,
                title=_payload_str(event, "title"),
                status="active",
                started_at=event.timestamp,
                baseline_ref=_payload_opt_str(event, "baseline_ref"),
                session_ids=[event.session_id] if event.session_id else [],
                last_seq=event.seq,
                updated_at=event.timestamp,
            )
        elif kind in _LIFECYCLE_TARGET:
            state = _require_task(states, event, kind)
            target = _LIFECYCLE_TARGET[kind]
            check_task_transition(state.status, target)
            state.status = target
            if target in ("completed", "cancelled"):
                state.finished_at = event.timestamp
                if kind == "task_completed":
                    state.final_ref = _payload_opt_str(event, "final_ref")
            state.last_seq = event.seq
            state.updated_at = event.timestamp
        elif kind == "checkpoint_created":
            state = _require_task(states, event, "checkpoint_created")
            if state.status in ("completed", "cancelled"):
                raise InvalidStateError(
                    f"checkpoint_created event {event.id} (seq={event.seq}) on terminal "
                    f"task {state.task_id} (status={state.status})"
                )
            index = _payload_int(event, "index")
            if index != state.checkpoint_count + 1:
                raise InvalidStateError(
                    f"checkpoint index regression/out-of-order for task {state.task_id}: "
                    f"expected {state.checkpoint_count + 1}, got {index} "
                    f"(event {event.id}, seq={event.seq})"
                )
            state.checkpoint_count = index
            state.next_checkpoint_index = index + 1
            _join_session(state, event.session_id)
            state.last_seq = event.seq
            state.updated_at = event.timestamp
        elif kind == "task_session_bound":
            state = _require_task(states, event, "task_session_bound")
            if state.status in ("completed", "cancelled"):
                raise InvalidStateError(
                    f"task_session_bound event {event.id} (seq={event.seq}) on terminal "
                    f"task {state.task_id} (status={state.status})"
                )
            if event.session_id is None:
                raise InvalidStateError(f"task_session_bound event {event.id} has no session_id")
            _join_session(state, event.session_id)
            state.last_seq = event.seq
            state.updated_at = event.timestamp
    return states


def project_session_states(events: Iterable[RawEvent]) -> dict[str, SessionRuntimeState]:
    """把 seq 有序事件流重放为 session_id -> SessionRuntimeState。"""
    sessions: dict[str, SessionRuntimeState] = {}
    for event in events:
        kind = event.kind
        session_id = event.session_id
        if kind == "session_started":
            if session_id is None:
                raise InvalidStateError(f"session_started event {event.id} has no session_id")
            if session_id in sessions:
                raise InvalidStateError(f"duplicate session_started for session {session_id}")
            sessions[session_id] = SessionRuntimeState(
                session_id=session_id,
                project_id=event.project_id,
                agent=_payload_str(event, "agent"),
                started_at=event.timestamp,
                external_session_id=_payload_opt_str(event, "external_session_id"),
                last_seq=event.seq,
                updated_at=event.timestamp,
            )
        elif kind == "session_ended":
            state = sessions.get(session_id or "")
            if state is None:
                raise InvalidStateError(
                    f"session_ended event {event.id} (seq={event.seq}) references unknown "
                    f"session {session_id!r}"
                )
            if state.ended_at is not None:
                raise InvalidStateError(f"duplicate session_ended for session {state.session_id}")
            state.ended_at = event.timestamp
            state.last_seq = event.seq
            state.updated_at = event.timestamp
        elif session_id is not None and kind in (
            "task_started",
            "task_session_bound",
            "checkpoint_created",
        ):
            state = sessions.get(session_id)
            if state is not None and event.task_id and event.task_id not in state.bound_task_ids:
                state.bound_task_ids.append(event.task_id)
    return sessions


def project_task_session_links(events: Iterable[RawEvent]) -> list[TaskSessionLink]:
    """重放 TaskSessionLink：join 来自 task_started / task_session_bound，
    left 来自 session 结束、Task 终态或同一 Session 转绑其他 Task。"""
    links: dict[tuple[str, str], TaskSessionLink] = {}
    order: list[tuple[str, str]] = []

    def close(task_id: str, session_id: str, at: datetime) -> None:
        link = links.get((task_id, session_id))
        if link is not None and link.left_at is None:
            link.left_at = at

    for event in events:
        kind = event.kind
        session_id = event.session_id
        task_id = event.task_id
        if kind == "task_started" and session_id and task_id:
            key = (task_id, session_id)
            if key not in links:
                links[key] = TaskSessionLink(
                    task_id=task_id, session_id=session_id, joined_at=event.timestamp
                )
                order.append(key)
        elif kind == "task_session_bound" and session_id and task_id:
            key = (task_id, session_id)
            if key not in links:
                links[key] = TaskSessionLink(
                    task_id=task_id, session_id=session_id, joined_at=event.timestamp
                )
                order.append(key)
            # 同一 Session 转绑其他 Task 时，旧 open link 关闭
            for (other_task, other_session) in order:
                if other_session == session_id and other_task != task_id:
                    close(other_task, session_id, event.timestamp)
        elif kind in _LIFECYCLE_TARGET and task_id:
            if _LIFECYCLE_TARGET[kind] in ("completed", "cancelled"):
                for (other_task, other_session) in order:
                    if other_task == task_id:
                        close(task_id, other_session, event.timestamp)
        elif kind == "session_ended" and session_id:
            for (other_task, other_session) in order:
                if other_session == session_id:
                    close(other_task, session_id, event.timestamp)
    return [links[key] for key in order]


def _join_session(state: TaskRuntimeState, session_id: str | None) -> None:
    if session_id and session_id not in state.session_ids:
        state.session_ids.append(session_id)


def _payload_value(event: RawEvent, key: str) -> Any:
    try:
        return event.payload[key]
    except KeyError as exc:
        raise InvalidStateError(
            f"{event.kind} event {event.id} payload missing required key {key!r}"
        ) from exc


def _payload_str(event: RawEvent, key: str) -> str:
    value = _payload_value(event, key)
    if not isinstance(value, str):
        raise InvalidStateError(
            f"{event.kind} event {event.id} payload key {key!r} must be a string, "
            f"got {type(value).__name__}"
        )
    return value


def _payload_opt_str(event: RawEvent, key: str) -> str | None:
    value = event.payload.get(key)
    if value is not None and not isinstance(value, str):
        raise InvalidStateError(
            f"{event.kind} event {event.id} payload key {key!r} must be a string or null"
        )
    return value


def _payload_int(event: RawEvent, key: str) -> int:
    value = _payload_value(event, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidStateError(
            f"{event.kind} event {event.id} payload key {key!r} must be an integer, "
            f"got {type(value).__name__}"
        )
    return value
