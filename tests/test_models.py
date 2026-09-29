"""领域模型测试：构造、校验、JSON round-trip、Task 状态机。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from project_evolution.errors import InvalidStateError, ValidationError
from project_evolution.ids import new_id
from project_evolution.models import (
    BugFix,
    Checkpoint,
    Decision,
    Evidence,
    EvolutionEvent,
    FeatureChange,
    Milestone,
    ParameterChange,
    Project,
    ProjectState,
    RawEvent,
    Session,
    Task,
    TaskSessionLink,
    Version,
    check_task_transition,
)

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 29, 10, 0, 0, tzinfo=TZ)


def make_project() -> Project:
    return Project(
        id=new_id("PRJ"),
        name="MouseTool",
        repository_root="C:/repo/mousetool",
        created_at=NOW,
        schema_version=1,
        git_remote="https://example.com/repo.git",
        default_branch="main",
    )


def make_session() -> Session:
    return Session(
        id=new_id("SES"),
        project_id=make_project().id,
        agent="codex",
        started_at=NOW,
    )


def make_task() -> Task:
    return Task(
        id=new_id("TASK"),
        project_id=make_project().id,
        title="实现外部窗口透明度",
        status="active",
        started_at=NOW,
        baseline_ref="abc123",
    )


def make_checkpoint() -> Checkpoint:
    return Checkpoint(
        id=new_id("CHK"),
        project_id=make_project().id,
        task_id=make_task().id,
        session_id=None,
        index=2,
        reason="milestone",
        summary="WindowOpacityService 核心逻辑完成",
        created_at=NOW,
        base_ref="abc123",
        head_ref="def456",
    )


def make_raw_event() -> RawEvent:
    return RawEvent(
        id=new_id("RAW"),
        project_id=make_project().id,
        timestamp=NOW,
        kind="file_modified",
        source="cli",
        payload={"path": "Core/WindowOpacityService.cs"},
    )


def make_evidence() -> Evidence:
    return Evidence(
        id=new_id("EVD"),
        project_id=make_project().id,
        type="git_commit",
        ref="abc123",
        created_at=NOW,
        metadata={"files_changed": 3},
    )


def make_evolution_event() -> EvolutionEvent:
    return EvolutionEvent(
        id=new_id("EVO"),
        project_id=make_project().id,
        task_id=make_task().id,
        checkpoint_id=make_checkpoint().id,
        timestamp=NOW,
        type="parameter_change",
        title="OpacityMin 0.2 -> 0.3",
        description="提高最低可见度",
        evidence_ids=[make_evidence().id],
        analyzer_version="mock-analyzer/0.1",
        payload={"name": "OpacityMin", "before": "0.2", "after": "0.3"},
    )


def make_milestone() -> Milestone:
    return Milestone(
        id=new_id("MS"),
        project_id=make_project().id,
        title="v0.3 交互改进",
        status="active",
        started_at=NOW,
    )


def make_version() -> Version:
    return Version(
        id=new_id("VER"),
        project_id=make_project().id,
        name="v0.2.0",
        git_tag="v0.2.0",
        git_commit="abc123",
        created_at=NOW,
    )


ALL_MODEL_FACTORIES = [
    make_project,
    make_session,
    make_task,
    make_checkpoint,
    make_raw_event,
    make_evidence,
    make_evolution_event,
    make_milestone,
    make_version,
    lambda: TaskSessionLink(
        task_id=make_task().id, session_id=make_session().id, joined_at=NOW
    ),
    lambda: FeatureChange(
        feature="External Window Opacity", action="added", scope="window interaction"
    ),
    lambda: ParameterChange(
        name="OpacityMin", before="0.2", after="0.3", reason="提高最低可见度"
    ),
    lambda: Decision(
        subject="窗口透明度快捷键",
        decision="使用 Alt+Wheel",
        reason="保留普通滚轮原生行为",
        supersedes=[],
        alternatives=["Ctrl+Wheel"],
    ),
    lambda: BugFix(
        problem="Hook回调中窗口查询可能造成输入卡顿",
        cause="同步窗口调用阻塞Hook线程",
        fix="将窗口操作调度到UI线程",
        affected_scope=["MouseHook"],
        verification="build passed",
    ),
    lambda: ProjectState(active_features=["External Window Opacity"]),
]


@pytest.mark.parametrize("factory", ALL_MODEL_FACTORIES)
def test_construct_and_json_round_trip(factory) -> None:
    obj = factory()
    dumped = obj.model_dump_json()
    restored = type(obj).model_validate_json(dumped)
    assert restored == obj


def test_datetime_serialized_as_iso8601_with_timezone() -> None:
    data = make_raw_event().model_dump(mode="json")
    assert data["timestamp"] == "2026-09-29T10:00:00+08:00"
    restored = RawEvent.model_validate(data)
    assert restored.timestamp.utcoffset() == timedelta(hours=8)


def test_task_session_link_many_to_many() -> None:
    # 一个 Task 可跨多个 Session，一个 Session 可参与多个 Task
    task_id = make_task().id
    links = [
        TaskSessionLink(task_id=task_id, session_id=make_session().id, joined_at=NOW)
        for _ in range(3)
    ]
    assert len({link.session_id for link in links}) == 3


# ------------------------------------------------------------- envelope 约定


def test_raw_event_seq_defaults_to_none() -> None:
    assert make_raw_event().seq is None


def test_evolution_event_seq_defaults_to_none() -> None:
    assert make_evolution_event().seq is None


def test_raw_event_schema_version_defaults_to_1() -> None:
    assert make_raw_event().schema_version == 1


def test_raw_event_known_kind_requires_payload_fields() -> None:
    with pytest.raises(PydanticValidationError):
        RawEvent(
            id=new_id("RAW"),
            project_id=make_project().id,
            timestamp=NOW,
            kind="file_modified",
            source="cli",
            payload={"old_path": "a"},
        )


def test_raw_event_unknown_kind_allowed() -> None:
    event = RawEvent(
        id=new_id("RAW"),
        project_id=make_project().id,
        timestamp=NOW,
        kind="something_new",
        source="cli",
        payload={},
    )
    assert event.kind == "something_new"


# ------------------------------------------------------------ required 字段缺失


@pytest.mark.parametrize(
    ("factory", "drop"),
    [
        (make_project, "repository_root"),
        (make_session, "agent"),
        (make_task, "title"),
        (make_checkpoint, "index"),
        (make_raw_event, "source"),
        (make_evidence, "ref"),
        (make_evolution_event, "evidence_ids"),
        (make_evolution_event, "analyzer_version"),
        (make_milestone, "status"),
        (make_version, "name"),
    ],
)
def test_missing_required_field_rejected(factory, drop: str) -> None:
    data = factory().model_dump()
    del data[drop]
    with pytest.raises(PydanticValidationError):
        type(factory()).model_validate(data)


# --------------------------------------------------------- 非法 enum / status


def test_task_invalid_status_rejected() -> None:
    data = make_task().model_dump()
    data["status"] = "done"
    with pytest.raises(PydanticValidationError):
        Task.model_validate(data)


def test_milestone_invalid_status_rejected() -> None:
    data = make_milestone().model_dump()
    data["status"] = "ongoing"
    with pytest.raises(PydanticValidationError):
        Milestone.model_validate(data)


def test_evidence_invalid_type_rejected() -> None:
    data = make_evidence().model_dump()
    data["type"] = "chat_log"
    with pytest.raises(PydanticValidationError):
        Evidence.model_validate(data)


def test_evolution_event_invalid_type_rejected() -> None:
    data = make_evolution_event().model_dump()
    data["type"] = "refactor"
    with pytest.raises(PydanticValidationError):
        EvolutionEvent.model_validate(data)


def test_feature_change_invalid_action_rejected() -> None:
    data = FeatureChange(feature="X", action="added").model_dump()
    data["action"] = "renamed"
    with pytest.raises(PydanticValidationError):
        FeatureChange.model_validate(data)


# ------------------------------------------------------------- Task 状态机


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("active", "paused"),
        ("active", "completed"),
        ("active", "cancelled"),
        ("paused", "active"),
        ("paused", "completed"),
        ("paused", "cancelled"),
        ("completed", "completed"),  # 终态幂等：重复 complete 不产生新事件
    ],
)
def test_task_allowed_transitions(current: str, target: str) -> None:
    check_task_transition(current, target)  # 不抛异常


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("active", "active"),
        ("completed", "active"),
        ("completed", "paused"),
        ("completed", "cancelled"),
        ("paused", "paused"),
        ("cancelled", "active"),
        ("cancelled", "paused"),
        ("cancelled", "completed"),
        ("cancelled", "cancelled"),  # cancelled 拒绝一切生命周期调用
    ],
)
def test_task_forbidden_transitions(current: str, target: str) -> None:
    with pytest.raises(InvalidStateError):
        check_task_transition(current, target)


@pytest.mark.parametrize("status", ["running", "", "ACTIVE"])
def test_task_transition_unknown_status_rejected(status: str) -> None:
    with pytest.raises(ValidationError):
        check_task_transition(status, "completed")
    with pytest.raises(ValidationError):
        check_task_transition("active", status)
