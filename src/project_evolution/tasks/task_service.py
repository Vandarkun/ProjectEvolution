"""TaskService：Task 生命周期（start / pause / resume / complete / cancel）。

状态机严格按 02-data-model.md §5 的固定转换表，实现不自行扩展：

```text
active    → paused | completed | cancelled
paused    → active | completed | cancelled
completed → 终态（重复 complete 幂等返回，不产生新事件）
cancelled → 终态（拒绝一切生命周期调用）
```

每个生命周期动作都追加对应 RawEvent（03-event-model.md §3），
Canonical 状态由 Ledger 投影重建；runtime journal 只是镜像。
Git baseline 通过 BaselineRecorder 端口注入（phase-03 §5：接口占位，Phase 04 实现）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from project_evolution.errors import InvalidStateError, TaskNotFoundError, ValidationError
from project_evolution.ids import new_id
from project_evolution.models import RawEvent, Task
from project_evolution.models.task import check_task_transition
from project_evolution.tasks.ports import BaselineRecorder
from project_evolution.tasks.session_service import create_session
from project_evolution.tasks.state import TaskRuntimeState
from project_evolution.tasks.store import TaskStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskService:
    def __init__(self, store: TaskStore, *, baseline_recorder: BaselineRecorder | None = None):
        self._store = store
        self._baseline_recorder = baseline_recorder

    # ------------------------------------------------------------------ read

    def get_task(self, task_id: str) -> Task | None:
        state = self._store.project_task(task_id)
        return state.to_task() if state is not None else None

    def list_tasks(self) -> list[Task]:
        states = self._store.project_tasks().values()
        return [
            state.to_task()
            for state in sorted(states, key=lambda s: (s.started_at, s.task_id))
        ]

    # ----------------------------------------------------------------- write

    def start_task(
        self,
        project_id: str,
        title: str,
        agent: str | None = None,
        session_id: str | None = None,
        *,
        source: str = "cli",
    ) -> Task:
        """开始 Task：校验冲突 → 创建 Task → 可选绑定 Session → task_started → journal。

        - ``agent`` 给定而 ``session_id`` 为空：自动创建一个该 agent 的 Session；
        - ``session_id`` 给定：必须是已存在、未结束、未绑定其他活跃 Task 的 Session。
        """
        if not project_id:
            raise ValidationError("project_id must be a non-empty string")
        if not title or not title.strip():
            raise ValidationError("title must be a non-empty string")

        with self._store.locked():
            new_session_id: str | None = None
            if session_id is not None:
                session = self._store.project_session(session_id)
                if session is None:
                    raise ValidationError(f"session not found: {session_id}")
                if agent is not None and session.agent != agent:
                    raise ValidationError(
                        f"session {session_id} belongs to agent {session.agent!r}, "
                        f"not {agent!r}"
                    )
                self._reject_ended_session(session_id, session.ended_at)
                self._reject_conflicting_binding(session_id)
                new_session_id = session_id
            elif agent is not None:
                created = create_session(self._store, project_id, agent, source=source)
                new_session_id = created.id

            task_id = new_id("TASK")
            baseline = (
                self._baseline_recorder.record_task_baseline(task_id)
                if self._baseline_recorder is not None
                else None
            )
            baseline_ref = baseline.head_ref if baseline is not None else None
            payload: dict = {"title": title, "baseline_ref": baseline_ref}
            if baseline is not None:
                payload["baseline_branch"] = baseline.branch
                payload["baseline_degraded"] = baseline.degraded
                if baseline.snapshot_path:
                    payload["baseline_snapshot_path"] = baseline.snapshot_path

            event = RawEvent(
                id=new_id("RAW"),
                project_id=project_id,
                timestamp=_now(),
                kind="task_started",
                source=source,
                task_id=task_id,
                session_id=new_session_id,
                payload=payload,
            )
            self._store.append_assuming_locked([event])
            state = TaskRuntimeState(
                task_id=task_id,
                project_id=project_id,
                title=title,
                status="active",
                started_at=event.timestamp,
                baseline_ref=baseline_ref,
                session_ids=[new_session_id] if new_session_id else [],
                last_seq=event.seq,
                updated_at=event.timestamp,
            )
            self._store.journal.write(state)
            return state.to_task()

    def pause_task(
        self, task_id: str, reason: str | None = None, *, source: str = "cli"
    ) -> Task:
        return self._transition(task_id, "task_paused", "paused", reason, source=source)

    def resume_task(
        self, task_id: str, reason: str | None = None, *, source: str = "cli"
    ) -> Task:
        return self._transition(task_id, "task_resumed", "active", reason, source=source)

    def cancel_task(
        self, task_id: str, reason: str | None = None, *, source: str = "cli"
    ) -> Task:
        return self._transition(task_id, "task_cancelled", "cancelled", reason, source=source)

    def complete_task(
        self, task_id: str, summary: str | None = None, *, source: str = "cli"
    ) -> Task:
        """完成 Task。对已完成 Task 幂等返回原状态，不产生新事件。"""
        with self._store.locked():
            state = self._require_task(task_id)
            if state.status == "completed":
                return state.to_task()  # 幂等：不追加事件
            check_task_transition(state.status, "completed")
            event = RawEvent(
                id=new_id("RAW"),
                project_id=state.project_id,
                timestamp=_now(),
                kind="task_completed",
                source=source,
                task_id=task_id,
                payload={"summary": summary, "final_ref": None},
            )
            self._store.append_assuming_locked([event])
            state.status = "completed"
            state.finished_at = event.timestamp
            state.last_seq = event.seq
            state.updated_at = event.timestamp
            self._store.journal.write(state)
            return state.to_task()

    # ----------------------------------------------------------------- inside

    def _transition(
        self,
        task_id: str,
        kind: str,
        target: str,
        reason: str | None,
        *,
        source: str = "cli",
    ) -> Task:
        with self._store.locked():
            state = self._require_task(task_id)
            check_task_transition(state.status, target)
            event = RawEvent(
                id=new_id("RAW"),
                project_id=state.project_id,
                timestamp=_now(),
                kind=kind,
                source=source,
                task_id=task_id,
                payload={"reason": reason},
            )
            self._store.append_assuming_locked([event])
            state.status = target  # type: ignore[assignment]
            if target in ("completed", "cancelled"):
                state.finished_at = event.timestamp
            state.last_seq = event.seq
            state.updated_at = event.timestamp
            self._store.journal.write(state)
            return state.to_task()

    def _require_task(self, task_id: str) -> TaskRuntimeState:
        state = self._store.project_task(task_id)
        if state is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return state

    def _reject_ended_session(self, session_id: str, ended_at: datetime | None) -> None:
        if ended_at is not None:
            raise InvalidStateError(
                f"cannot start task in ended session {session_id}"
            )

    def _reject_conflicting_binding(self, session_id: str) -> None:
        """start_task 的冲突校验：session 已 open 绑定到其他非终态 Task 时拒绝。"""
        tasks = self._store.project_tasks()
        for link in self._store.project_links():
            if link.session_id != session_id or link.left_at is not None:
                continue
            other = tasks.get(link.task_id)
            if other is not None and other.status not in ("completed", "cancelled"):
                raise InvalidStateError(
                    f"session {session_id} is already bound to active task "
                    f"{link.task_id} (status={other.status}); end it or start the new task "
                    f"with another session"
                )
