"""Query Service 的 Timeline / 列表查询测试（phase-08，10 §4）。

复用 test_query_service 的种子数据（Task-1 透明度 / Task-2 Hook）：

- Timeline 混合 task / checkpoint / feature / parameter / decision / bug，
  按时间正序；File Event 不进入 Timeline；
- kind 过滤（短名 + 事件类型名 + 未知值报错）；limit 取最新 N 条；
- list_tasks / list_changed_files / list_*_histories / get_evidence_record。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_evolution.index import (
    TIMELINE_KINDS,
    ProjectionNotBuiltError,
    QueryService,
    rebuild_projection,
)
from test_query_service import PRJ


def test_timeline_mixes_kinds_in_time_order(query_service: QueryService) -> None:
    """默认 Timeline 混合全部 kind，时间正序，File Event 不出现。"""
    items = query_service.get_timeline()
    kinds = {item.kind for item in items}
    assert kinds == {"task", "checkpoint", "feature", "parameter", "decision", "bug"}
    timestamps = [item.timestamp for item in items]
    assert timestamps == sorted(timestamps)
    # 两条 task lifecycle（TASK-1 start / TASK-2 start + complete）
    task_items = [item for item in items if item.kind == "task"]
    assert [item.task_id for item in task_items] == ["TASK-1", "TASK-2", "TASK-2"]
    assert any(item.title.startswith("Task completed:") for item in task_items)
    # checkpoint 条目携带 Evidence 计数（CHK-1/CHK-2 都有 diff evidence）
    checkpoints = [item for item in items if item.kind == "checkpoint"]
    assert len(checkpoints) == 2
    assert all(item.evidence_count == 1 for item in checkpoints)
    # 语义事件携带 event_id / evidence_count（追溯 + 下钻）
    param = next(item for item in items if item.kind == "parameter")
    assert param.event_id.startswith("EVO-")
    assert param.evidence_count == 1
    # 低层文件事件绝不进入 Timeline
    assert not any("MouseHook" in item.title for item in items)


def test_timeline_kind_filters(query_service: QueryService) -> None:
    only_param = query_service.get_timeline(["parameter"])
    assert only_param and {item.kind for item in only_param} == {"parameter"}

    # 事件类型名与短名等价
    by_type_name = query_service.get_timeline(["parameter_change", "bug_fix"])
    assert {item.kind for item in by_type_name} == {"parameter", "bug"}

    # version 在 V0.1 恒为空，但 kind 合法
    assert query_service.get_timeline(["version"]) == []
    assert set(TIMELINE_KINDS) >= {"task", "feature", "parameter", "decision", "bug", "version"}

    with pytest.raises(ValueError, match="unknown timeline kind"):
        query_service.get_timeline(["file", "bogus"])


def test_timeline_limit_keeps_latest(query_service: QueryService) -> None:
    all_items = query_service.get_timeline()
    tail = query_service.get_timeline(limit=3)
    assert tail == all_items[-3:]
    with pytest.raises(ValueError, match="limit"):
        query_service.get_timeline(limit=0)


def test_list_tasks_with_status_filter(query_service: QueryService) -> None:
    tasks = query_service.list_tasks()
    assert [brief.task_id for brief in tasks] == ["TASK-1", "TASK-2"]
    completed = query_service.list_tasks(status="completed")
    assert [brief.task_id for brief in completed] == ["TASK-2"]
    assert completed[0].finished_at is not None
    assert query_service.list_tasks(status="cancelled") == []


def test_list_changed_files_orders_by_count(query_service: QueryService) -> None:
    files = query_service.list_changed_files()
    # MouseHook.cs: added+modified+renamed 出现在两条 path 行（rename 前后名）
    by_path = {summary.path: summary.change_count for summary in files}
    assert by_path["Core/MouseHook.cs"] == 2
    assert by_path["Core/HookManager.cs"] == 1
    counts = [summary.change_count for summary in files]
    assert counts == sorted(counts, reverse=True)
    assert query_service.list_changed_files(limit=1) == files[:1]


def test_list_histories_cover_all_entities(query_service: QueryService) -> None:
    parameters = query_service.list_parameter_histories()
    assert [history.name for history in parameters] == ["OpacityMin"]
    assert parameters[0].current == "4"  # 跨 scope 最新 after

    features = query_service.list_feature_histories()
    assert {history.name for history in features} == {"窗口透明度", "Hook 配置"}
    by_name = {history.name: history for history in features}
    assert by_name["窗口透明度"].current == "absent"
    assert by_name["Hook 配置"].current == "present"

    decisions = query_service.list_decision_histories()
    assert len(decisions) == 1
    assert decisions[0].current is not None
    assert decisions[0].current.decision == "Alt+Wheel"


def test_evidence_record_from_projection(query_service: QueryService) -> None:
    record = query_service.get_evidence_record("EVD-1")
    assert record is not None
    assert record.type == "git_diff"
    assert record.task_id == "TASK-1"
    assert record.checkpoint_id == "CHK-1"
    assert record.metadata["base_ref"] is None or isinstance(
        record.metadata["base_ref"], str
    )
    assert query_service.get_evidence_record("EVD-NOPE") is None


def test_timeline_requires_built_projection(tmp_path: Path) -> None:
    project = tmp_path / "empty-project"
    project.mkdir()
    with pytest.raises(ProjectionNotBuiltError):
        QueryService(project).get_timeline()


def test_timeline_ignores_ledger_not_projection(
    query_service: QueryService, tmp_path: Path
) -> None:
    """Timeline 只读 SQLite 投影：账本追加而未追平时保持旧结果。"""
    from project_evolution.ledger import RawEventLedger
    from project_evolution.models import RawEvent

    before = len(query_service.get_timeline(["task"]))
    raw = RawEventLedger(query_service._root)  # noqa: SLF001 - 测试内直查内部根
    raw.append(
        RawEvent(
            id="RAW-T9START00000000000000000",
            project_id=PRJ,
            timestamp=query_service.get_timeline()[-1].timestamp,
            kind="task_started",
            source="test",
            task_id="TASK-9",
            payload={"title": "later"},
        )
    )
    assert len(query_service.get_timeline(["task"])) == before
    rebuild_projection(query_service._root)  # noqa: SLF001
    assert len(query_service.get_timeline(["task"])) == before + 1
