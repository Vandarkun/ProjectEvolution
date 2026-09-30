"""CheckpointService：长任务的阶段性 Checkpoint。

- Checkpoint index 在 Task 内从 1 严格递增（phase-03 §6）；
- 每次产生 checkpoint_created RawEvent（payload: index/reason/summary）；
- 终态 Task 拒绝 checkpoint（completed → checkpoint ×，cancelled 同样拒绝）；
- Git 证据通过 CheckpointCollector 端口注入
  （phase-03 占位为空实现，Phase 04 实现快照对比 + diff evidence）；
- ``on_checkpoint`` 挂钩在写锁释放后异步触发（phase-05：语义分析默认异步，
  Checkpoint 事实落盘即返回，不等待模型）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from project_evolution.errors import InvalidStateError, TaskNotFoundError
from project_evolution.ids import new_id
from project_evolution.models import Checkpoint, RawEvent
from project_evolution.tasks.ports import CheckpointCollector
from project_evolution.tasks.state import TaskRuntimeState
from project_evolution.tasks.store import TaskStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CheckpointService:
    def __init__(
        self,
        store: TaskStore,
        *,
        collector: CheckpointCollector | None = None,
        on_checkpoint: Callable[[str, str], None] | None = None,
    ):
        self._store = store
        self._collector = collector
        self._on_checkpoint = on_checkpoint

    # ------------------------------------------------------------------ read

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        for event in self._store.replay():
            if event.kind == "checkpoint_created" and event.checkpoint_id == checkpoint_id:
                return _checkpoint_from_event(event)
        return None

    def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        """按 checkpoint index 升序返回（读取端按 seq 重放后天然有序）。"""
        return [
            _checkpoint_from_event(event)
            for event in self._store.replay()
            if event.kind == "checkpoint_created"
            and event.task_id == task_id
            and event.checkpoint_id is not None
        ]

    # ----------------------------------------------------------------- write

    def create_checkpoint(
        self,
        task_id: str,
        summary: str | None = None,
        reason: str | None = None,
        session_id: str | None = None,
        *,
        source: str = "cli",
    ) -> Checkpoint:
        """创建 Checkpoint：采集证据 → checkpoint_created → file events → journal。

        写锁释放后触发 ``on_checkpoint(task_id, checkpoint_id)`` 挂钩
        （后台语义分析）；挂钩异常不影响 Checkpoint 结果。
        """
        with self._store.locked():
            state = self._require_task(task_id)
            if state.status in ("completed", "cancelled"):
                raise InvalidStateError(
                    f"cannot create checkpoint on terminal task {task_id} "
                    f"(status={state.status})"
                )
            if session_id is not None:
                self._validate_session(state, session_id)

            checkpoint_id = new_id("CHK")
            index = state.next_checkpoint_index
            evidence = (
                self._collector.collect_checkpoint_evidence(
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_index=index,
                    session_id=session_id,
                    source=source,
                )
                if self._collector is not None
                else None
            )
            payload: dict = {
                "index": index,
                "reason": reason,
                "summary": summary,
            }
            if evidence is not None:
                payload["base_ref"] = evidence.base_ref
                payload["head_ref"] = evidence.head_ref
                payload["scope_confidence"] = evidence.scope_confidence
                if evidence.snapshot_path:
                    payload["snapshot_path"] = evidence.snapshot_path
                if evidence.diff_evidence is not None:
                    payload["diff_evidence_id"] = evidence.diff_evidence.id
                    payload["diff_evidence_sha256"] = evidence.diff_evidence.ref

            event = RawEvent(
                id=new_id("RAW"),
                project_id=state.project_id,
                timestamp=_now(),
                kind="checkpoint_created",
                source=source,
                task_id=task_id,
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                payload=payload,
            )
            events = [event]
            if evidence is not None:
                events.extend(evidence.file_events)
            self._store.append_assuming_locked(events)

            state.checkpoint_count = index
            state.next_checkpoint_index = index + 1
            if session_id and session_id not in state.session_ids:
                state.session_ids.append(session_id)
            state.last_seq = events[-1].seq
            state.updated_at = event.timestamp
            self._store.journal.write(state)
            checkpoint = _checkpoint_from_event(event)

        if self._on_checkpoint is not None:
            self._on_checkpoint(task_id, checkpoint.id)
        return checkpoint

    # ----------------------------------------------------------------- inside

    def _require_task(self, task_id: str) -> TaskRuntimeState:
        state = self._store.project_task(task_id)
        if state is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return state

    def _validate_session(self, state: TaskRuntimeState, session_id: str) -> None:
        session = self._store.project_session(session_id)
        if session is None:
            raise InvalidStateError(f"session not found: {session_id}")
        if session.project_id != state.project_id:
            raise InvalidStateError(
                f"session {session_id} belongs to project {session.project_id}, "
                f"task {state.task_id} to {state.project_id}"
            )
        if session.ended_at is not None:
            raise InvalidStateError(
                f"cannot create checkpoint with ended session {session_id}"
            )


def _checkpoint_from_event(event: RawEvent) -> Checkpoint:
    assert event.checkpoint_id is not None
    payload = event.payload
    return Checkpoint(
        id=event.checkpoint_id,
        project_id=event.project_id,
        task_id=event.task_id or "",
        session_id=event.session_id,
        index=payload["index"],
        reason=payload.get("reason"),
        summary=payload.get("summary"),
        created_at=event.timestamp,
        base_ref=payload.get("base_ref"),
        head_ref=payload.get("head_ref"),
    )
