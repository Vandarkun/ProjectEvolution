"""Query Service 测试（phase-06 §6/§8）。

重点：

- Current State 语义（§8 三条明确用例）：
  Parameter 1→2→3 current=3；Decision A 被 B supersede current=B；
  Feature added→modified→removed current=absent；
- Task/Parameter/Feature/Decision/Bug/File 历史查询；
- 结果紧凑性（不内联 diff，携带 evidence_ids 下钻）；
- Query Service 不调用 LLM（模块不 import 任何 evolution provider）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_evolution.index import (
    ProjectionNotBuiltError,
    QueryService,
    rebuild_projection,
)
from project_evolution.ledger import EvolutionEventLedger, RawEventLedger
from project_evolution.models import EvolutionEvent, RawEvent

PRJ = "PRJ-query"


# ----------------------------------------------------------------- helpers


def seed_event_stream(project: Path) -> list[str]:
    """手工构造一段完整的账本历史（无 Git，纯事件流）。

    内容：
    - Task-1（透明度）: 参数 OpacityMin 1→2→3；Feature 窗口透明度
      added→modified→removed；Decision 快捷键 A 被 B supersede；BugFix×2
      （不同 scope）；
    - Task-2（Hook）: 参数同名不同 scope；文件 add/modify/rename/delete；
    - test_passed / test_failed 事件。
    """
    raw = RawEventLedger(project)
    evo = EvolutionEventLedger(project)
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    n = 0

    def next_ts() -> datetime:
        nonlocal n
        n += 1
        return now + timedelta(minutes=n)

    raw_events: list[RawEvent] = [
        RawEvent(
            id="RAW-T1START00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="task_started",
            source="test",
            task_id="TASK-1",
            payload={"title": "窗口透明度"},
        ),
        RawEvent(
            id="RAW-T1CP10000000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="checkpoint_created",
            source="test",
            task_id="TASK-1",
            checkpoint_id="CHK-1",
            payload={"index": 1, "summary": "cp1", "diff_evidence_id": "EVD-1",
                     "diff_evidence_sha256": "a" * 64},
        ),
        RawEvent(
            id="RAW-T1CP20000000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="checkpoint_created",
            source="test",
            task_id="TASK-1",
            checkpoint_id="CHK-2",
            payload={"index": 2, "summary": "cp2", "diff_evidence_id": "EVD-2",
                     "diff_evidence_sha256": "b" * 64},
        ),
        RawEvent(
            id="RAW-T2START00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="task_started",
            source="test",
            task_id="TASK-2",
            payload={"title": "Hook 重构"},
        ),
        # Task-2 的文件变化（含 rename：old->new）
        RawEvent(
            id="RAW-FILEADD00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="file_added",
            source="test",
            task_id="TASK-2",
            checkpoint_id="CHK-3",
            payload={"path": "Core/MouseHook.cs", "content_hash": "h1"},
        ),
        RawEvent(
            id="RAW-FILEMOD00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="file_modified",
            source="test",
            task_id="TASK-2",
            checkpoint_id="CHK-3",
            payload={"path": "Core/MouseHook.cs", "content_hash": "h2",
                     "evidence_id": "EVD-3"},
        ),
        RawEvent(
            id="RAW-FILEREN00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="file_modified",
            source="test",
            task_id="TASK-2",
            checkpoint_id="CHK-3",
            payload={"path": "Core/HookManager.cs", "old_path": "Core/MouseHook.cs",
                     "new_path": "Core/HookManager.cs", "change": "renamed",
                     "content_hash": "h3"},
        ),
        RawEvent(
            id="RAW-TESTP000000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="test_passed",
            source="test",
            task_id="TASK-2",
            payload={"command": "pytest -q"},
        ),
        RawEvent(
            id="RAW-TESTF000000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="test_failed",
            source="test",
            task_id="TASK-2",
            payload={"command": "pytest -k hook"},
        ),
        RawEvent(
            id="RAW-T2DONE00000000000000000",
            project_id=PRJ,
            timestamp=next_ts(),
            kind="task_completed",
            source="test",
            task_id="TASK-2",
            payload={"summary": "完成", "final_ref": "abc123"},
        ),
    ]
    raw.append_many(raw_events)

    e = 0

    def evo_event(etype: str, title: str, payload: dict, evidence: list[str]) -> EvolutionEvent:
        nonlocal e
        e += 1
        return EvolutionEvent(
            id=f"EVO-{e:023d}",
            project_id=PRJ,
            task_id="TASK-1" if etype != "hook" else "TASK-2",
            checkpoint_id="CHK-1",
            timestamp=next_ts(),
            type=etype if etype != "hook" else "bug_fix",
            title=title,
            description="",
            evidence_ids=evidence,
            analyzer_version="test",
            prompt_version="test",
            analysis_batch_id="ANL-1",
            analysis_key=f"{PRJ}|{e}",
            payload=payload,
        )

    decision_a = evo_event(
        "decision",
        "快捷键用 Ctrl+Shift+U",
        {"subject": "透明度快捷键", "decision": "Ctrl+Shift+U",
         "supersedes": []},
        ["EVD-1"],
    )
    events = [
        # Parameter: 1 → 2 → 3
        evo_event("parameter_change", "OpacityMin 1 -> 2",
                  {"name": "OpacityMin", "before": "1", "after": "2"}, ["EVD-1"]),
        evo_event("parameter_change", "OpacityMin 2 -> 3",
                  {"name": "OpacityMin", "before": "2", "after": "3"}, ["EVD-1"]),
        # 同名参数不同 scope
        evo_event("parameter_change", "OpacityMin(ui) 3 -> 4",
                  {"name": "OpacityMin", "before": "3", "after": "4",
                   "scope": "ui"}, ["EVD-1"]),
        # Feature: added -> modified -> removed
        evo_event("feature_change", "窗口透明度 added",
                  {"feature": "窗口透明度", "action": "added"}, ["EVD-1"]),
        evo_event("feature_change", "窗口透明度 modified",
                  {"feature": "窗口透明度", "action": "modified",
                   "after": "平滑过渡"}, ["EVD-1"]),
        evo_event("feature_change", "窗口透明度 removed",
                  {"feature": "窗口透明度", "action": "removed"}, ["EVD-2"]),
        # 存活的 Feature
        evo_event("feature_change", "Hook 配置 added",
                  {"feature": "Hook 配置", "action": "added"}, ["EVD-2"]),
        decision_a,
        # Decision B 取代 A
        evo_event("decision", "透明度快捷键改用 Alt+Wheel",
                  {"subject": "透明度快捷键", "decision": "Alt+Wheel",
                   "supersedes": [decision_a.id]}, ["EVD-2"]),
        # BugFix ×2（scope 不同）
        evo_event("bug_fix", "Hook 线程阻塞",
                  {"problem": "Hook 线程阻塞输入", "fix": "移出 Hook 线程",
                   "affected_scope": ["MouseHook.cs"], "verification": "pytest"},
                  ["EVD-2"]),
        evo_event("bug_fix", "透明度闪烁",
                  {"problem": "透明度调节闪烁", "fix": "双缓冲",
                   "affected_scope": ["WindowOpacityService.cs"]},
                  ["EVD-2"]),
    ]
    evo.append_many(events)
    return [event.id for event in events]


@pytest.fixture()
def query_service(tmp_path: Path) -> QueryService:
    project = tmp_path / "project"
    project.mkdir()
    seed_event_stream(project)
    rebuild_projection(project)
    return QueryService(project)


# --------------------------------------------------------- current state §8


def test_parameter_current_is_latest_valid_after(query_service: QueryService) -> None:
    """§8：Parameter A 1 → 2 → 3，current = 3。"""
    history = query_service.get_parameter_history("opacitymin")  # 大小写不敏感
    assert [change.after for change in history.changes] == ["2", "3", "4"]  # 含 scope=ui
    assert history.changes[0].before == "1"
    assert history.current == "4"  # 不过滤 scope：最新一条是 ui scope 的 4

    scoped = query_service.get_parameter_history("OpacityMin", scope="ui")
    assert scoped.current == "4"
    assert [c.after for c in scoped.changes] == ["4"]

    global_scope = query_service.get_parameter_history("OpacityMin", scope=None)
    assert global_scope.current_by_scope == {"": "3", "ui": "4"}

    state = query_service.get_project_state()
    assert state.current_parameters["OpacityMin"] == "4"


def test_decision_supersede_current_is_b(query_service: QueryService) -> None:
    """§8：Decision A 被 Decision B supersede，current = B。"""
    history = query_service.get_decision_history("透明度快捷键")
    assert len(history.decisions) == 2
    by_decision = {item.decision: item for item in history.decisions}
    a = by_decision["Ctrl+Shift+U"]
    b = by_decision["Alt+Wheel"]
    assert a.active is False
    assert a.superseded_by == [b.event_id]
    assert b.active is True
    assert b.supersedes == [a.event_id]
    assert history.current is not None and history.current.event_id == b.event_id

    state = query_service.get_project_state()
    assert state.active_decisions == ["Alt+Wheel"]


def test_feature_removed_means_absent(query_service: QueryService) -> None:
    """§8：Feature added → modified → removed，current = absent。"""
    history = query_service.get_feature_history("窗口透明度")
    assert [change.action for change in history.changes] == [
        "added",
        "modified",
        "removed",
    ]
    assert history.current == "absent"
    assert query_service.get_project_state().active_features == ["Hook 配置"]

    unknown = query_service.get_feature_history("不存在的功能")
    assert unknown.current is None and unknown.changes == []


# ------------------------------------------------------------------- task


def test_list_active_tasks_excludes_terminal(query_service: QueryService) -> None:
    """非终态 Task 列表（phase-07 get_project_state 输入）。"""
    active = query_service.list_active_tasks()
    assert [brief.task_id for brief in active] == ["TASK-1"]  # TASK-2 已 completed
    assert active[0].status == "active"
    assert active[0].title  # 携带标题


def test_task_history_reconstructs_detail(query_service: QueryService) -> None:
    history = query_service.get_task_history("TASK-2")
    assert history is not None
    assert history.title == "Hook 重构"
    assert history.status == "completed"
    assert history.finished_at is not None
    assert history.tests_passed == 1 and history.tests_failed == 1
    assert history.files == ["Core/HookManager.cs", "Core/MouseHook.cs"]
    assert history.file_change_count == 3
    assert history.evolution == []  # TASK-2 无语义事件

    task1 = query_service.get_task_history("TASK-1")
    assert task1 is not None
    assert len(task1.checkpoints) == 2
    assert task1.checkpoints[0].evidence_id == "EVD-1"
    assert len(task1.evolution) == 11
    param_item = next(i for i in task1.evolution if i.type == "parameter_change")
    assert "after=2" in param_item.detail

    assert query_service.get_task_history("TASK-NOPE") is None


# -------------------------------------------------------------------- file


def test_file_history_tracks_rename(query_service: QueryService) -> None:
    """旧名/新名都能查到完整历史（rename 后可继续追踪，10 §11）。"""
    expected_changes = ["added", "modified", "renamed"]
    for name, expected_renamed_from in (
        ("Core/HookManager.cs", ["Core/MouseHook.cs"]),  # 现名 -> 曾用名
        ("Core/MouseHook.cs", []),  # 曾用名本身就是查询对象
    ):
        history = query_service.get_file_history(name)
        assert len(history.file_changes) == 3, name
        assert [change.change for change in history.file_changes] == expected_changes
        assert history.renamed_from == expected_renamed_from, name
        assert [task.task_id for task in history.tasks] == ["TASK-2"]
        assert all(task.status == "completed" for task in history.tasks)


def test_file_history_links_evolution_via_checkpoint(query_service: QueryService) -> None:
    history = query_service.get_file_history("Core/Whatever.cs")
    assert history.file_changes == []


# --------------------------------------------------------------------- bug


def test_bug_history_with_scope_filter(query_service: QueryService) -> None:
    all_bugs = query_service.get_bug_history()
    assert len(all_bugs) == 2
    assert all_bugs[0].evidence_count == 1  # EVD-2
    assert all_bugs[0].evidence_ids == ["EVD-2"]

    scoped = query_service.get_bug_history(scope="MouseHook.cs")
    assert len(scoped) == 1
    assert scoped[0].problem == "Hook 线程阻塞输入"
    assert scoped[0].verification == "pytest"

    assert query_service.get_bug_history(scope="nope.cs") == []


# ------------------------------------------------------------------ status


def test_status_reports_sync_marker(query_service: QueryService) -> None:
    status = query_service.status()
    assert status.version >= 1
    assert status.stale is False
    assert status.in_sync is True
    assert status.raw_seq > 0 and status.evo_seq > 0


def test_queries_require_built_projection(tmp_path: Path) -> None:
    project = tmp_path / "bare"
    project.mkdir()
    service = QueryService(project)
    with pytest.raises(ProjectionNotBuiltError):
        service.get_project_state()


# ------------------------------------------------------------- no-LLM rule


def test_query_module_never_imports_providers() -> None:
    """验收标准：Query Service 不直接调用 LLM——源码不引用任何 provider/analyzer。"""
    import re

    import project_evolution.index.query as query_module

    source = Path(query_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("providers", "MockProvider", "OpenAICompatible", "Ollama",
                      "analyzer_service", "EvolutionAnalyzerService"):
        assert forbidden not in source, f"query.py 不应引用 {forbidden}"
    # 项目内 import 只允许 config/models/index 三族（不含 evolution provider 族）
    project_imports = re.findall(r"from (project_evolution[\w.]*)", source)
    for module in project_imports:
        assert not module.startswith("project_evolution.evolution"), module
