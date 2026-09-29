"""Session / Checkpoint 测试（phase-03 §12）：跨 Session、5-checkpoint 长任务、
checkpoint 顺序、绑定校验、index 回退检测。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_evolution.errors import InvalidStateError, SessionNotFoundError, ValidationError
from project_evolution.ids import new_id
from project_evolution.models import RawEvent
from project_evolution.tasks import create_task_services

PRJ = "PRJ-test"


def events_of(services, kind: str) -> list[RawEvent]:
    return [event for event in services.store.replay() if event.kind == kind]


# ------------------------------------------------------------------ checkpoint


def test_five_checkpoint_long_task(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "long task")
    for i in range(1, 6):
        cp = services.checkpoints.create_checkpoint(
            task.id, summary=f"cp-{i}", reason="milestone"
        )
        assert cp.index == i
        assert cp.id.startswith("CHK-")
        assert cp.task_id == task.id

    checkpoints = services.checkpoints.list_checkpoints(task.id)
    assert [cp.index for cp in checkpoints] == [1, 2, 3, 4, 5]
    created = events_of(services, "checkpoint_created")
    assert [event.payload["index"] for event in created] == [1, 2, 3, 4, 5]
    assert created[2].payload["summary"] == "cp-3"
    assert created[2].payload["reason"] == "milestone"
    assert created[2].checkpoint_id == checkpoints[2].id


def test_checkpoint_order_stable_across_service_restart(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    services.checkpoints.create_checkpoint(task.id)
    # 新的进程/服务实例继续，index 不回退
    fresh = create_task_services(tmp_path)
    cp2 = fresh.checkpoints.create_checkpoint(task.id)
    assert cp2.index == 2
    assert [cp.index for cp in fresh.checkpoints.list_checkpoints(task.id)] == [1, 2]


def test_checkpoint_on_completed_task_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    services.tasks.complete_task(task.id)
    with pytest.raises(InvalidStateError, match="terminal"):
        services.checkpoints.create_checkpoint(task.id)


def test_checkpoint_index_regression_detected(tmp_path: Path) -> None:
    """绕过服务直接注入回退 index 的事件，投影必须显式报错而不是静默接受。"""
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    services.checkpoints.create_checkpoint(task.id)
    services.checkpoints.create_checkpoint(task.id)

    timestamp = datetime.now(timezone.utc)
    injected = RawEvent(
        id=new_id("RAW"),
        project_id=PRJ,
        timestamp=timestamp,
        kind="checkpoint_created",
        source="cli",
        task_id=task.id,
        checkpoint_id=new_id("CHK"),
        payload={"index": 2, "reason": None, "summary": "regressed"},  # 回退到 2
    )
    services.store.ledger.append(injected)

    fresh = create_task_services(tmp_path)
    with pytest.raises(InvalidStateError, match="regression"):
        fresh.checkpoints.create_checkpoint(task.id)
    with pytest.raises(InvalidStateError, match="regression"):
        fresh.store.project_tasks()


# --------------------------------------------------------- session continuation


def test_session_switch_continuation(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    session_a = services.sessions.start_session(PRJ, "codex")
    task = services.tasks.start_task(PRJ, "t", session_id=session_a.id)
    services.checkpoints.create_checkpoint(task.id, summary="cp1", session_id=session_a.id)

    services.sessions.end_session(session_a.id, reason="session A 结束")
    # Task 不因 Session 结束自动完成
    still = services.tasks.get_task(task.id)
    assert still is not None and still.status == "active"

    session_b = services.sessions.start_session(PRJ, "claude")
    link = services.sessions.bind_task(task.id, session_b.id)
    assert link.task_id == task.id
    assert link.session_id == session_b.id
    assert link.left_at is None

    cp2 = services.checkpoints.create_checkpoint(task.id, summary="cp2", session_id=session_b.id)
    assert cp2.session_id == session_b.id
    assert cp2.index == 2

    bound = events_of(services, "task_session_bound")
    assert len(bound) == 1
    assert bound[0].session_id == session_b.id
    assert bound[0].payload["agent"] == "claude"

    links = services.store.project_links()
    by_session = {link.session_id: link for link in links}
    assert by_session[session_a.id].left_at is not None  # A 已离开
    assert by_session[session_b.id].left_at is None
    assert services.tasks.get_task(task.id).status == "active"


def test_auto_session_from_agent(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t", agent="codex")
    started = events_of(services, "task_started")[0]
    assert started.session_id is not None
    assert started.session_id.startswith("SES-")
    session_started = events_of(services, "session_started")
    assert len(session_started) == 1
    assert session_started[0].payload["agent"] == "codex"
    assert session_started[0].seq < started.seq  # session 先创建
    assert services.tasks.get_task(task.id).status == "active"


def test_conflicting_start_task_rejected(tmp_path: Path) -> None:
    """Session 已 open 绑定另一个活跃 Task 时，禁止用它 start 新 Task。"""
    services = create_task_services(tmp_path)
    session = services.sessions.start_session(PRJ, "codex")
    services.tasks.start_task(PRJ, "t1", session_id=session.id)
    with pytest.raises(InvalidStateError, match="already bound"):
        services.tasks.start_task(PRJ, "t2", session_id=session.id)
    # 结束 session 后，新 session 可以继续；旧 session 不能再绑定
    services.sessions.end_session(session.id)
    session_b = services.sessions.start_session(PRJ, "codex")
    task2 = services.tasks.start_task(PRJ, "t2", session_id=session_b.id)
    with pytest.raises(InvalidStateError, match="ended"):
        services.sessions.bind_task(task2.id, session.id)


def test_end_session_idempotent(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    session = services.sessions.start_session(PRJ, "codex")
    first = services.sessions.end_session(session.id)
    count = len(services.store.replay())
    second = services.sessions.end_session(session.id)
    assert second.ended_at == first.ended_at
    assert len(services.store.replay()) == count
    with pytest.raises(SessionNotFoundError):
        services.sessions.end_session("SES-missing")


def test_bind_validations(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    session = services.sessions.start_session(PRJ, "codex")

    with pytest.raises(SessionNotFoundError):
        services.sessions.bind_task(task.id, "SES-missing")

    other_prj_session = None
    services2 = create_task_services(tmp_path / "other")  # 无关 store，仅制造记录
    del other_prj_session, services2

    # 同一 session 重复绑定同一 task：幂等
    link1 = services.sessions.bind_task(task.id, session.id)
    count = len(services.store.replay())
    link2 = services.sessions.bind_task(task.id, session.id)
    assert link2.joined_at == link1.joined_at
    assert len(services.store.replay()) == count

    # 结束后的 session 不能绑定新 task
    services.sessions.end_session(session.id)
    task2 = services.tasks.start_task(PRJ, "t2")
    with pytest.raises(InvalidStateError, match="ended"):
        services.sessions.bind_task(task2.id, session.id)

    # 终态 task 不能被绑定
    task3 = services.tasks.start_task(PRJ, "t3")
    services.tasks.complete_task(task3.id)
    session_c = services.sessions.start_session(PRJ, "claude")
    with pytest.raises(InvalidStateError, match="terminal"):
        services.sessions.bind_task(task3.id, session_c.id)


def test_cross_project_binding_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task("PRJ-a", "t")
    session = services.sessions.start_session("PRJ-b", "codex")
    with pytest.raises(ValidationError, match="different projects"):
        services.sessions.bind_task(task.id, session.id)


def test_checkpoint_with_ended_session_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    session = services.sessions.start_session(PRJ, "codex")
    task = services.tasks.start_task(PRJ, "t", session_id=session.id)
    services.sessions.end_session(session.id)
    with pytest.raises(InvalidStateError, match="ended"):
        services.checkpoints.create_checkpoint(task.id, session_id=session.id)


def test_agent_mismatch_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    session = services.sessions.start_session(PRJ, "codex")
    with pytest.raises(ValidationError, match="belongs to agent"):
        services.tasks.start_task(PRJ, "t", agent="claude", session_id=session.id)
