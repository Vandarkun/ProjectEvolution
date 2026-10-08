"""FastAPI read-only API 测试（phase-08 §3，11 §4）。

- 四个必做屏幕的数据端点（Overview / Timeline / Task Detail / Parameter）；
- 全部路由只读（GET，无 POST/PUT/DELETE）；
- 404 / 422 / 503（未建投影）语义；
- Evidence 下钻返回纯文本 Patch。
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="dashboard extra")

from project_evolution.dashboard import create_app  # noqa: E402
from project_evolution.index import QueryService  # noqa: E402


@pytest.fixture()
def client(query_service: QueryService):
    from fastapi.testclient import TestClient

    app = create_app(query_service.root)
    with TestClient(app) as test_client:
        yield test_client


def test_api_routes_are_read_only(client) -> None:
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if path.startswith(("/api", "/tasks", "/timeline", "/parameters", "/evidence")) or (
            path == "/"
        ):
            methods = getattr(route, "methods", set()) or set()
            assert methods <= {"GET", "HEAD"}, f"{path} exposes {methods}"


def test_api_overview(client) -> None:
    payload = client.get("/api/overview").json()
    assert payload["task_total"] == 2
    assert payload["task_completed"] == 1
    assert len(payload["active_tasks"]) == 1
    assert payload["latest_version"] is None
    for kind in ("feature", "parameter", "bug", "decision"):
        assert payload["recent"][kind]["event_id"].startswith(("EVO-", "RAW-"))
    assert payload["projection"]["in_sync"] is True


def test_api_timeline_with_kind_filter(client) -> None:
    payload = client.get("/api/timeline").json()
    assert payload["count"] > 0
    kinds = {item["kind"] for item in payload["items"]}
    assert "parameter" in kinds

    only_bugs = client.get("/api/timeline", params={"kinds": "bug"}).json()
    assert {item["kind"] for item in only_bugs["items"]} == {"bug"}

    limited = client.get("/api/timeline", params={"kinds": "parameter", "limit": 2}).json()
    assert limited["count"] == 2

    bad = client.get("/api/timeline", params={"kinds": "bogus"})
    assert bad.status_code == 422


def test_api_task_detail_and_404(client) -> None:
    payload = client.get("/api/tasks/TASK-1").json()
    assert payload["title"] == "窗口透明度"
    assert len(payload["checkpoints"]) == 2
    assert payload["tests_passed"] == 0  # 测试事件挂在 TASK-2
    task2 = client.get("/api/tasks/TASK-2").json()
    assert task2["tests_passed"] == 1 and task2["tests_failed"] == 1
    assert task2["files"] == ["Core/HookManager.cs", "Core/MouseHook.cs"]
    assert client.get("/api/tasks/TASK-NOPE").status_code == 404


def test_api_parameter_history(client) -> None:
    payload = client.get("/api/parameters/OpacityMin").json()
    assert [c["after"] for c in payload["changes"]] == ["2", "3", "4"]
    assert payload["current"] == "4"
    scoped = client.get("/api/parameters/OpacityMin", params={"scope": "ui"}).json()
    assert scoped["current"] == "4"
    assert [c["after"] for c in scoped["changes"]] == ["4"]


def test_api_other_entities(client) -> None:
    tasks = client.get("/api/tasks").json()
    assert tasks["count"] == 2
    feature = client.get("/api/features/Hook 配置").json()
    assert feature["current"] == "present"
    decision = client.get("/api/decisions/透明度快捷键").json()
    assert decision["current"]["decision"] == "Alt+Wheel"
    bugs = client.get("/api/bugs").json()
    assert bugs["count"] == 2
    scoped = client.get("/api/bugs", params={"scope": "MouseHook.cs"}).json()
    assert scoped["count"] == 1


def test_api_evidence_drill_down(client) -> None:
    payload = client.get("/api/evidence/EVD-1").json()
    assert payload["type"] == "git_diff"
    assert payload["task_id"] == "TASK-1"
    assert payload["checkpoint_id"] == "CHK-1"
    # 种子数据没写 patch 文件：内容为空但记录可下钻
    assert payload["content"] is None
    assert client.get("/api/evidence/EVD-NOPE").status_code == 404


def test_api_serves_html_pages(client) -> None:
    overview = client.get("/")
    assert overview.status_code == 200
    assert "text/html" in overview.headers["content-type"]
    timeline = client.get("/timeline")
    assert timeline.status_code == 200
    assert "Timeline" in timeline.text
    task = client.get("/tasks/TASK-1")
    assert task.status_code == 200 and "TASK-1" in task.text
    parameter = client.get("/parameters", params={"name": "OpacityMin"})
    assert parameter.status_code == 200
    assert "CURRENT" in parameter.text


def test_api_fresh_project_auto_builds_empty_projection(tmp_path) -> None:
    """未建投影的项目：首个请求自动增量重建（与 MCP 查询路径一致），返回空概览。"""
    from fastapi.testclient import TestClient

    project = tmp_path / "fresh"
    project.mkdir()
    with TestClient(create_app(project)) as test_client:
        response = test_client.get("/api/overview")
        assert response.status_code == 200
        assert response.json()["task_total"] == 0
        assert response.json()["active_tasks"] == []
