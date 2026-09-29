"""Task 生命周期测试（phase-03 §12）：状态机、RawEvent、幂等、非法转换、journal 恢复。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_evolution.errors import InvalidStateError, TaskNotFoundError, ValidationError
from project_evolution.ids import new_id
from project_evolution.models import RawEvent
from project_evolution.tasks import TaskServices, create_task_services

PRJ = "PRJ-test"


def kinds(services: TaskServices) -> list[str]:
    return [event.kind for event in services.store.replay()]


def events_of(services: TaskServices, kind: str) -> list[RawEvent]:
    return [event for event in services.store.replay() if event.kind == kind]


# ------------------------------------------------------------------ short task


def test_start_task_creates_active_task_and_event(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "实现外部窗口透明度")
    assert task.status == "active"
    assert task.title == "实现外部窗口透明度"
    assert task.project_id == PRJ
    assert task.baseline_ref is None  # phase-03 占位
    assert task.finished_at is None
    assert task.id.startswith("TASK-")

    started = events_of(services, "task_started")
    assert len(started) == 1
    assert started[0].task_id == task.id
    assert started[0].seq == 1  # seq 是 Canonical 排序键
    assert started[0].payload["title"] == "实现外部窗口透明度"
    assert started[0].payload["baseline_ref"] is None
    assert started[0].source == "cli"


def test_short_task_start_then_complete(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "short task")
    completed = services.tasks.complete_task(task.id, summary="完成")
    assert completed.status == "completed"
    assert completed.finished_at is not None

    assert kinds(services) == ["task_started", "task_completed"]
    done = events_of(services, "task_completed")[0]
    assert done.task_id == task.id
    assert done.payload == {"summary": "完成", "final_ref": None}
    assert done.seq == 2


def test_start_task_validation(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    with pytest.raises(ValidationError, match="title"):
        services.tasks.start_task(PRJ, "  ")
    with pytest.raises(ValidationError, match="project_id"):
        services.tasks.start_task("", "t")
    with pytest.raises(ValidationError, match="session not found"):
        services.tasks.start_task(PRJ, "t", session_id="SES-missing")


# ------------------------------------------------------- pause / resume / cancel


def test_pause_resume_cycle(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    paused = services.tasks.pause_task(task.id, reason="等待外部依赖")
    assert paused.status == "paused"
    resumed = services.tasks.resume_task(task.id, reason="依赖就绪")
    assert resumed.status == "active"

    paused_events = events_of(services, "task_paused")
    resumed_events = events_of(services, "task_resumed")
    assert paused_events[0].payload == {"reason": "等待外部依赖"}
    assert resumed_events[0].payload == {"reason": "依赖就绪"}
    assert kinds(services) == ["task_started", "task_paused", "task_resumed"]


def test_cancel_task_is_terminal(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    cancelled = services.tasks.cancel_task(task.id, reason="需求取消")
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None
    event = events_of(services, "task_cancelled")[0]
    assert event.payload == {"reason": "需求取消"}


def test_invalid_transitions_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    active = services.tasks.start_task(PRJ, "t1")
    # active -> active（resume 一个 active task）非法
    with pytest.raises(InvalidStateError, match="illegal task status transition"):
        services.tasks.resume_task(active.id)
    # active -> active 之外的重复 pause 也一样：先 pause 再 pause
    paused = services.tasks.pause_task(active.id)
    with pytest.raises(InvalidStateError, match="illegal task status transition"):
        services.tasks.pause_task(paused.id)
    # completed -> pause / cancelled -> complete
    done = services.tasks.start_task(PRJ, "t2")
    services.tasks.complete_task(done.id)
    with pytest.raises(InvalidStateError, match="illegal task status transition"):
        services.tasks.pause_task(done.id)
    dropped = services.tasks.start_task(PRJ, "t3")
    services.tasks.cancel_task(dropped.id)
    with pytest.raises(InvalidStateError, match="illegal task status transition"):
        services.tasks.complete_task(dropped.id)


def test_cancelled_task_rejects_all_lifecycle_calls(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    session = services.sessions.start_session(PRJ, "codex")
    services.sessions.bind_task(task.id, session.id)
    services.tasks.cancel_task(task.id)
    before = len(services.store.replay())

    with pytest.raises(InvalidStateError):
        services.tasks.pause_task(task.id)
    with pytest.raises(InvalidStateError):
        services.tasks.resume_task(task.id)
    with pytest.raises(InvalidStateError):
        services.tasks.complete_task(task.id)
    with pytest.raises(InvalidStateError):
        services.tasks.cancel_task(task.id)
    with pytest.raises(InvalidStateError):
        services.checkpoints.create_checkpoint(task.id)
    with pytest.raises(InvalidStateError):
        services.sessions.bind_task(task.id, session.id)

    assert len(services.store.replay()) == before  # 没有任何事件被追加


def test_duplicate_complete_is_idempotent(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    first = services.tasks.complete_task(task.id, summary="done")
    count_before = len(services.store.replay())
    second = services.tasks.complete_task(task.id, summary="again")
    assert second.status == "completed"
    assert second.finished_at == first.finished_at
    assert len(services.store.replay()) == count_before  # 不产生新事件


def test_nonexistent_task_rejected(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    assert services.tasks.get_task("TASK-missing") is None
    with pytest.raises(TaskNotFoundError):
        services.tasks.pause_task("TASK-missing")
    with pytest.raises(TaskNotFoundError):
        services.tasks.complete_task("TASK-missing")
    with pytest.raises(TaskNotFoundError):
        services.checkpoints.create_checkpoint("TASK-missing")
    session = services.sessions.start_session(PRJ, "codex")
    with pytest.raises(TaskNotFoundError):
        services.sessions.bind_task("TASK-missing", session.id)


# --------------------------------------------------------- runtime journal 恢复


def test_runtime_journal_deleted_recovers_from_ledger(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    services.checkpoints.create_checkpoint(task.id, summary="cp1")
    services.checkpoints.create_checkpoint(task.id, summary="cp2")
    journal_path = tmp_path / ".project-evolution" / ".runtime" / "tasks"
    assert journal_path.is_dir()
    # journal 是唯一被删除的东西；Ledger 原样保留
    import shutil

    shutil.rmtree(journal_path)

    fresh = create_task_services(tmp_path)
    recovered = fresh.tasks.get_task(task.id)
    assert recovered is not None
    assert recovered.status == "active"
    assert recovered.title == "t"
    checkpoints = fresh.checkpoints.list_checkpoints(task.id)
    assert [cp.index for cp in checkpoints] == [1, 2]

    cp3 = fresh.checkpoints.create_checkpoint(task.id, summary="cp3")
    assert cp3.index == 3  # index 不因 journal 丢失而回退
    # journal 被重新生成
    assert (journal_path / task.id / "state.json").is_file()
    state = json.loads((journal_path / task.id / "state.json").read_text(encoding="utf-8"))
    assert state["checkpoint_count"] == 3


def test_corrupt_journal_falls_back_to_ledger(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    journal_file = (
        tmp_path / ".project-evolution" / ".runtime" / "tasks" / task.id / "state.json"
    )
    journal_file.write_text("{ not json", encoding="utf-8")
    fresh = create_task_services(tmp_path)
    assert fresh.tasks.get_task(task.id) is not None
    assert fresh.tasks.get_task(task.id).status == "active"


# ------------------------------------------------ ledger 一致性 / envelope 校验


def test_lifecycle_events_form_valid_ledger(tmp_path: Path) -> None:
    services = create_task_services(tmp_path)
    task = services.tasks.start_task(PRJ, "t")
    services.tasks.pause_task(task.id)
    services.tasks.resume_task(task.id)
    services.checkpoints.create_checkpoint(task.id, summary="cp")
    services.tasks.complete_task(task.id, summary="done")

    report = services.store.ledger.check_integrity()
    assert report.ok, report.issues
    seqs = [event.seq for event in services.store.replay()]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
    payload_error = dict(
        id=new_id("RAW"),
        project_id=PRJ,
        timestamp=services.store.replay()[0].timestamp,
        kind="task_started",
        source="cli",
        payload={},  # 缺 title
    )
    with pytest.raises(Exception, match="title"):
        RawEvent(**payload_error)
