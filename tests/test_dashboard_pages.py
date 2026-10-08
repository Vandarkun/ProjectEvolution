"""Dashboard 页面安全与隔离测试（phase-08 §8/§10，11 §9）。

XSS：恶意 commit message（经 checkpoint summary / patch）、task title、
语义事件标题 / 描述在页面与 Evidence 下钻中只显示文本，不出现原始
``<script>`` 标签。

隔离（§10 最后一条）：Dashboard 关闭后 Core / MCP 仍正常 ——
页面访问期间账本文件字节不变（只读证明）；退出后 Ledger 追加、
MCP 工具查询、投影重建照常工作。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient", reason="dashboard extra")

from fastapi.testclient import TestClient  # noqa: E402

from project_evolution.dashboard import create_app  # noqa: E402
from project_evolution.index import QueryService, rebuild_projection  # noqa: E402
from project_evolution.ledger import EvolutionEventLedger, RawEventLedger  # noqa: E402
from project_evolution.models import EvolutionEvent, RawEvent  # noqa: E402

SCRIPT = "<script>alert(1)</script>"
IMG_ONERROR = '<img src=x onerror="alert(2)">'
PRJ = "PRJ-xss"


def _seed_malicious(project: Path) -> None:
    """带 XSS 载荷的事件流：task 标题 / checkpoint summary（commit message
    落点）/ 语义事件标题与描述 / bug 字段。"""
    raw = RawEventLedger(project)
    evo = EvolutionEventLedger(project)
    now = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=1)

    raw.append_many([
        RawEvent(
            id="RAW-XSTART000000000000000000",
            project_id=PRJ,
            timestamp=now,
            kind="task_started",
            source="test",
            task_id="TASK-X",
            payload={"title": f"标题 {SCRIPT}"},
        ),
        RawEvent(
            id="RAW-XCHK0000000000000000000",
            project_id=PRJ,
            timestamp=now + step,
            kind="checkpoint_created",
            source="test",
            task_id="TASK-X",
            checkpoint_id="CHK-X",
            payload={
                "index": 1,
                "summary": f"commit: {SCRIPT}",
                "diff_evidence_id": "EVD-X",
                "diff_evidence_sha256": "c" * 64,
            },
        ),
    ])
    evo.append(
        EvolutionEvent(
            id="EVO-XBUG0000000000000000000",
            project_id=PRJ,
            task_id="TASK-X",
            checkpoint_id="CHK-X",
            timestamp=now + 2 * step,
            type="bug_fix",
            title=f"Bug {IMG_ONERROR}",
            description=f"描述 {SCRIPT}",
            evidence_ids=["EVD-X"],
            analyzer_version="test",
            prompt_version="test",
            analysis_batch_id="ANL-X",
            analysis_key=f"{PRJ}|1",
            payload={
                "problem": f"problem {SCRIPT}",
                "fix": f"fix {IMG_ONERROR}",
                "affected_scope": ["a.cs"],
            },
        )
    )
    # 恶意 patch 文件（Evidence 下钻的 <pre> 内容）
    diffs = project / ".project-evolution" / "evidence" / "diffs"
    diffs.mkdir(parents=True, exist_ok=True)
    (diffs / "EVD-X.patch").write_text(
        f"diff --git a/a.cs b/a.cs\n--- commit msg: {SCRIPT}\n"
        f"+var x = '{SCRIPT}';\n+html: {IMG_ONERROR}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def xss_client(tmp_path: Path):
    project = tmp_path / "xss-project"
    project.mkdir()
    _seed_malicious(project)
    rebuild_projection(project)
    with TestClient(create_app(project)) as client:
        yield client, project


def _assert_no_raw_script(html: str) -> None:
    """页面不得包含未转义的载荷标签；转义后的纯文本（&lt;…&gt;）是安全形态。"""
    for payload in (SCRIPT, IMG_ONERROR):
        assert payload not in html, f"raw payload leaked: {payload}"
    body_without_style = re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)
    for raw_tag in ("<script", "<img", "<svg", "<iframe", "<body onload"):
        assert raw_tag not in body_without_style.lower(), raw_tag
    # 载荷一定以转义形态出现（页面确实展示了这些文本）
    assert "&lt;script&gt;" in html or "&lt;img src=x onerror=" in html


def test_overview_page_escapes_task_title(xss_client) -> None:
    client, _ = xss_client
    html = client.get("/").text
    _assert_no_raw_script(html)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_timeline_page_escapes_checkpoint_and_events(xss_client) -> None:
    client, _ = xss_client
    html = client.get("/timeline").text
    _assert_no_raw_script(html)
    # checkpoint summary（commit message 落点）与事件标题都出现（转义后）
    assert "commit: &lt;script&gt;" in html


def test_task_detail_escapes_all_sections(xss_client) -> None:
    client, _ = xss_client
    html = client.get("/tasks/TASK-X").text
    _assert_no_raw_script(html)
    assert "&lt;img src=x onerror=" in html  # img 载荷同样只显示文本


def test_evidence_page_shows_patch_as_plain_text(xss_client) -> None:
    client, _ = xss_client
    response = client.get("/evidence/EVD-X")
    assert response.status_code == 200
    _assert_no_raw_script(response.text)
    assert "&lt;script&gt;" in response.text


def test_parameter_page_escapes_reflected_query(xss_client) -> None:
    """未知参数名的 404 detail 不进入 HTML 页面（JSON 错误体，亦无执行面）。"""
    client, _ = xss_client
    response = client.get("/parameters", params={"name": SCRIPT})
    assert response.status_code == 404
    assert SCRIPT not in response.text or response.headers[
        "content-type"
    ].startswith("application/json")


def test_json_api_never_executes(xss_client) -> None:
    """JSON API 返回载荷文本（application/json 不被浏览器执行）。"""
    client, _ = xss_client
    response = client.get("/api/tasks/TASK-X")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert SCRIPT in response.text  # 原文返回，但 JSON 无脚本执行面


# --------------------------------------------------------------- isolation


def test_dashboard_stop_leaves_core_and_mcp_healthy(tmp_path: Path) -> None:
    """§10：Dashboard 停止后 Core / MCP 仍正常；运行期间账本只读。"""
    project = tmp_path / "iso-project"
    project.mkdir()
    raw_path = project / ".project-evolution" / "events" / "raw.jsonl"
    evo_path = project / ".project-evolution" / "events" / "evolution.jsonl"
    _seed_malicious(project)
    rebuild_projection(project)
    before_raw = raw_path.read_bytes()
    before_evo = evo_path.read_bytes()

    with TestClient(create_app(project)) as client:
        for path in ("/", "/timeline", "/tasks/TASK-X", "/api/overview", "/evidence/EVD-X"):
            assert client.get(path).status_code == 200, path
        # Dashboard 全程未改账本（只读证明）
        assert raw_path.read_bytes() == before_raw
        assert evo_path.read_bytes() == before_evo

    # “停止”之后：Core 写入照常
    raw = RawEventLedger(project)
    raw.append(
        RawEvent(
            id="RAW-AFTER000000000000000000",
            project_id=PRJ,
            timestamp=datetime(2026, 7, 2, tzinfo=timezone.utc),
            kind="task_started",
            source="test",
            task_id="TASK-AFTER",
            payload={"title": "after dashboard closed"},
        )
    )
    assert raw_path.read_bytes() != before_raw

    # MCP 工具查询照常（get_project_state 走同一 QueryService）
    from project_evolution.mcp import PelToolService, ProjectRegistry

    service = PelToolService(ProjectRegistry.single(project), source="test")
    state = service.get_project_state({})
    assert len(state["active_tasks"]) == 2  # TASK-X + TASK-AFTER 均为 active
    service.shutdown()

    # 投影重建照常
    report = rebuild_projection(project)
    assert report.counts["tasks"] == 2
    timeline = QueryService(project).get_timeline(["task"])
    assert [item.task_id for item in timeline] == ["TASK-X", "TASK-AFTER"]
