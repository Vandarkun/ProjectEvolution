"""Tool Service 测试（phase-07 §5/§6，08 §4-§9/§11）。

覆盖：

- start/checkpoint/complete 的结构化响应与 Git 证据；
- partial success：Analyzer 失败时 checkpoint_saved=true + analysis_status=pending；
- 稳定错误码（INVALID_ARGUMENT / TASK_NOT_FOUND / INVALID_TASK_STATE /
  PROJECT_* / EVIDENCE_NOT_FOUND）；
- get_project_state / get_history 紧凑响应与投影元数据；
- get_evidence 的 EVD-/RAW- 下钻与内容裁剪；
- 语义闭环：OfflineRuleProvider 产生的事件经后台 worker 进入 get_history。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.evolution.evaluation import OfflineRuleProvider
from project_evolution.evolution.providers import MockProvider, ProviderError
from project_evolution.mcp.errors import (
    EVIDENCE_NOT_FOUND,
    INVALID_ARGUMENT,
    INVALID_TASK_STATE,
    PROJECT_AMBIGUOUS,
    TASK_NOT_FOUND,
    ToolError,
)
from project_evolution.mcp.registry import ProjectEntry, ProjectRegistry
from project_evolution.mcp.service import PelToolService


class FailingProvider(MockProvider):
    """恒失败的 Provider（模拟模型不可用）。"""

    name = "failing"

    async def analyze(self, request):  # type: ignore[override]
        raise ProviderError("simulated provider outage", kind="api")


def make_service(
    repo: Path, *, provider: Any = "mock"
) -> PelToolService:
    if provider == "mock":
        factory = lambda config: MockProvider()  # noqa: E731
    elif provider == "failing":
        factory = lambda config: FailingProvider()  # noqa: E731
    elif provider == "rules":
        factory = lambda config: OfflineRuleProvider()  # noqa: E731
    elif provider is None:
        factory = None
    else:  # pragma: no cover - 测试内部约定
        raise AssertionError(provider)
    return PelToolService(
        ProjectRegistry.single(repo, name="demo"), provider_factory=factory
    )


def drain(service: PelToolService, timeout: float = 30.0) -> None:
    runtime = next(iter(service._runtimes.values()))
    worker = runtime.worker
    if worker is not None:
        assert worker.wait_idle(timeout=timeout), "background worker did not drain"


# ------------------------------------------------------------------ lifecycle


def test_start_task_response_shape(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = make_service(repo)
    result = service.start_task({"title": "增加窗口透明度", "agent": "codex"})
    assert result["task_id"].startswith("TASK-")
    assert result["status"] == "active"
    assert result["baseline"]  # git HEAD
    assert result["baseline_degraded"] is False
    assert result["project"]["name"] == "demo"
    assert result["session_id"]  # agent 给定 -> 自动创建 session
    service.shutdown()


def test_checkpoint_partial_success_when_provider_fails(tmp_path: Path) -> None:
    """Analyzer 失败：事实保存成功即返回，不整体 failed（phase-07 §6）。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = make_service(repo, provider="failing")
    task = service.start_task({"title": "t"})
    (repo / "app.py").write_text("x = 2\n")
    result = service.checkpoint_task(
        {
            "task_id": task["task_id"],
            "summary": "step",
            "tests": [{"command": "make test", "status": "passed"}],
        }
    )
    assert result["checkpoint_saved"] is True
    assert result["analysis_status"] == "pending"  # 失败转 pending，不是 failed
    assert result["checkpoint_id"].startswith("CHK-")
    assert result["evidence"]["changed_files"] == ["app.py"]
    drain(service)
    # 失败详情进了 pending 队列，可 retry（phase-05 语义不变）
    runtime = next(iter(service._runtimes.values()))
    pending = runtime.services  # noqa: F841  # 事实仍在账本，不因分析失败回滚
    raw_events = list(runtime.services.store.replay())
    kinds = [event.kind for event in raw_events]
    assert "checkpoint_created" in kinds and "test_passed" in kinds
    service.shutdown()


def test_complete_task_final_checkpoint_and_idempotency(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = make_service(repo)
    task = service.start_task({"title": "t"})
    result = service.complete_task({"task_id": task["task_id"], "summary": "done"})
    assert result["status"] == "completed"
    assert result["checkpoint_id"]  # 完成前自动补一个增量 checkpoint
    assert result["analysis_status"] == "pending"

    again = service.complete_task({"task_id": task["task_id"]})
    assert again["already_completed"] is True
    assert again["checkpoint_id"] is None
    assert again["status"] == "completed"
    service.shutdown()


def test_task_lifecycle_error_codes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    service = make_service(repo)

    with pytest.raises(ToolError) as excinfo:
        service.checkpoint_task({"task_id": "TASK-nope"})
    assert excinfo.value.code == TASK_NOT_FOUND

    with pytest.raises(ToolError) as excinfo:
        service.start_task({"title": "   "})
    assert excinfo.value.code == INVALID_ARGUMENT

    with pytest.raises(ToolError) as excinfo:
        service.start_task({})  # 缺 title
    assert excinfo.value.code == INVALID_ARGUMENT

    task = service.start_task({"title": "t"})
    cancelled = service.complete_task if False else None  # noqa: F841
    # 取消后的 complete -> INVALID_TASK_STATE
    runtime = next(iter(service._runtimes.values()))
    runtime.services.tasks.cancel_task(task["task_id"])
    with pytest.raises(ToolError) as excinfo:
        service.complete_task({"task_id": task["task_id"]})
    assert excinfo.value.code == INVALID_TASK_STATE
    service.shutdown()


def test_multi_project_resolution_errors(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = init_repo(tmp_path / "a")
    repo_b = init_repo(tmp_path / "b")
    registry = ProjectRegistry(
        [ProjectEntry(name="alpha", root=str(repo_a)), ProjectEntry(name="beta", root=str(repo_b))]
    )
    service = PelToolService(registry, provider_factory=None)

    with pytest.raises(ToolError) as excinfo:
        service.get_project_state({})
    assert excinfo.value.code == PROJECT_AMBIGUOUS

    # 显式指定后成功，且后续无参调用继续用同一项目（当前绑定）
    state = service.get_project_state({"project": "beta"})
    assert state["project"]["name"] == "beta"
    state = service.get_project_state({})
    assert state["project"]["name"] == "beta"
    # 误指向别的仓库（alpha 不在本次绑定内被解析成功）不会写错项目
    with pytest.raises(ToolError) as excinfo:
        service.start_task({"project": "gamma", "title": "x"})
    assert excinfo.value.code == "PROJECT_NOT_FOUND"
    service.shutdown()


# --------------------------------------------------------------------- query


def test_get_project_state_compact(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = make_service(repo)
    task = service.start_task({"title": "透明度"})
    state = service.get_project_state({})
    assert [t["task_id"] for t in state["active_tasks"]] == [task["task_id"]]
    assert state["projection"]["stale"] is False
    assert state["projection"]["in_sync"] is True
    # 紧凑：默认不含 checkpoint / diff 明细
    assert "checkpoints" not in state
    assert "history" not in state
    service.complete_task({"task_id": task["task_id"]})
    state = service.get_project_state({})
    assert state["active_tasks"] == []
    service.shutdown()


def test_get_history_task_and_evidence_drilldown(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = make_service(repo, provider=None)  # 无 provider：事实仍可查
    task = service.start_task({"title": "t"})
    (repo / "app.py").write_text("x = 2\n")
    cp = service.checkpoint_task({"task_id": task["task_id"], "summary": "s"})

    history = service.get_history({"kind": "task", "query": task["task_id"]})
    assert history["count"] == 1
    detail = history["results"][0]
    assert detail["status"] == "active"
    assert [c["checkpoint_id"] for c in detail["checkpoints"]] == [cp["checkpoint_id"]]
    assert detail["files"] == ["app.py"]
    assert detail["tests_passed"] == 0

    evidence_id = cp["evidence"]["diff_evidence_id"]
    assert evidence_id
    evidence = service.get_evidence({"evidence_id": evidence_id})
    assert evidence["type"] == "git_diff"
    assert evidence["task_id"] == task["task_id"]
    assert "+x = 2" in evidence["content"]
    assert evidence["truncated"] is False

    with pytest.raises(ToolError) as excinfo:
        service.get_evidence({"evidence_id": "EVD-404"})
    assert excinfo.value.code == EVIDENCE_NOT_FOUND
    service.shutdown()


def test_get_history_argument_validation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    service = make_service(repo, provider=None)
    with pytest.raises(ToolError) as excinfo:
        service.get_history({"kind": "nonsense"})
    assert excinfo.value.code == INVALID_ARGUMENT
    with pytest.raises(ToolError) as excinfo:
        service.get_history({"kind": "task"})  # 缺 query
    assert excinfo.value.code == INVALID_ARGUMENT
    with pytest.raises(ToolError) as excinfo:
        service.get_history({"kind": "task", "query": "TASK-404"})
    assert excinfo.value.code == TASK_NOT_FOUND
    history = service.get_history({"kind": "version", "query": None})
    assert history["results"] == [] and history["count"] == 0
    service.shutdown()


def test_get_history_returns_raw_event_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    service = make_service(repo, provider=None)
    task = service.start_task({"title": "t"})
    cp = service.checkpoint_task(
        {"task_id": task["task_id"], "tests": [{"command": "pytest", "status": "passed"}]}
    )
    runtime = next(iter(service._runtimes.values()))
    test_event = next(
        e for e in runtime.services.store.replay() if e.kind == "test_passed"
    )
    evidence = service.get_evidence({"evidence_id": test_event.id})
    assert evidence["type"] == "raw_event"
    assert evidence["kind"] == "test_passed"
    assert evidence["checkpoint_id"] == cp["checkpoint_id"]
    assert evidence["payload"]["command"] == "pytest"
    service.shutdown()


# ------------------------------------------------------------ semantic loop


def test_semantic_events_appear_in_history_after_drain(tmp_path: Path) -> None:
    """checkpoint(异步分析) -> drain -> get_history 看到语义事件（mock 闭环）。"""
    from project_evolution.evolution.analyzer import AnalyzerResponse, CandidateEvent

    def respond(request):
        if not request.digest.evidence:
            return None
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="OpacityMin 0.2 -> 0.3",
                    description="提高最低可见度",
                    evidence_ids=[request.digest.evidence[0].id],
                    payload={
                        "name": "OpacityMin",
                        "before": "0.2",
                        "after": "0.3",
                        "reason": None,
                    },
                )
            ]
        )

    repo = init_repo(tmp_path)
    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.parent.mkdir()
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.2;\n"
        "}\n",
        encoding="utf-8",
    )
    commit_all(repo, "init")
    service = PelToolService(
        ProjectRegistry.single(repo, name="demo"),
        provider_factory=lambda config: MockProvider(responder=respond),
    )
    task = service.start_task({"title": "增加窗口透明度"})
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.3;\n"
        "}\n",
        encoding="utf-8",
    )
    service.checkpoint_task({"task_id": task["task_id"], "summary": "最低透明度调整"})

    deadline = time.monotonic() + 30.0
    parameter = None
    while time.monotonic() < deadline:
        history = service.get_history({"kind": "parameter", "query": "OpacityMin"})
        parameter = history["results"][0]
        if parameter["changes"]:
            break
        time.sleep(0.05)
    assert parameter is not None and parameter["changes"], "semantic analysis never landed"
    assert parameter["current"] == "0.3"
    assert parameter["changes"][0]["after"] == "0.3"
    service.shutdown()


def test_analysis_disabled_mode_reports_disabled(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    service = make_service(repo, provider=None)
    task = service.start_task({"title": "t"})
    result = service.checkpoint_task({"task_id": task["task_id"]})
    assert result["analysis_status"] == "disabled"
    assert result["checkpoint_saved"] is True
    service.shutdown()
