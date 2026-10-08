"""Evolution Report 导出测试（phase-08 §7，11 §6/§7/§8/§9）。

- 报告 10 节结构齐全；关键结论携带 Event ID（§8）；
- Parameter Evolution 显示 Before → After 与 CURRENT；
- Markdown / HTML 可独立生成；
- 恶意文本（``<script>alert(1)</script>``）在 HTML 中被转义、不出现原始标签；
- 数据组装只调用 QueryService（模块不 import ledger / git）。
"""

from __future__ import annotations

import re

from project_evolution.dashboard import build_report
from project_evolution.dashboard.export import render_html, render_markdown
from project_evolution.index import QueryService

SCRIPT = "<script>alert(1)</script>"


def test_report_has_ten_sections(query_service: QueryService) -> None:
    report = build_report(query_service)
    markdown = render_markdown(report)
    for index in range(1, 11):
        assert f"## {index}." in markdown, f"missing section {index}"
    assert "Project Evolution Report" in markdown


def test_markdown_carries_event_ids_and_current(query_service: QueryService) -> None:
    report = build_report(query_service)
    markdown = render_markdown(report)

    # §8：OpacityMin 0.25 → 0.30 [EVO-x] 形式（before → after + Event ID）
    param_rows = [line for line in markdown.splitlines() if "→" in line and "[EVO-" in line]
    assert param_rows, "parameter rows must carry event ids"
    assert any("**(CURRENT)**" in row for row in param_rows)
    assert re.search(r"OpacityMin", markdown)
    # 每条语义事件都能对上 Event ID（抽查 BugFix 与 Decision）
    assert any(f"`[{bug.event_id}]`" in markdown for bug in report.bugs)
    assert any(
        f"`[{item.event_id}]`" in markdown
        for history in report.decisions
        for item in history.decisions
    )
    # Decision supersede 链可见
    assert "superseded by" in markdown


def test_markdown_escapes_table_pipes(query_service: QueryService) -> None:
    """表格单元格内的 ``|`` 不破坏 Markdown 表格（转义为 \\|）。"""
    report = build_report(query_service)
    report.tasks[0].title = "a|b|c"
    markdown = render_markdown(report)
    assert "a\\|b\\|c" in markdown


def test_html_export_escapes_malicious_text(query_service: QueryService) -> None:
    """恶意 title/summary：HTML 导出只显示文本，不出现原始 <script>。"""
    report = build_report(query_service)
    # 注入：task 标题 / 参数 reason / bug problem
    report.tasks[0].title = SCRIPT
    report.parameters[0].changes[0].reason = SCRIPT
    if report.bugs:
        report.bugs[0].problem = SCRIPT
    html = render_html(report)

    assert SCRIPT not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # 事件里的 HTML/JS 不被浏览器执行：无未转义的 script 开始标签（样式块除外）
    assert not re.search(r"<script", html, flags=re.IGNORECASE)


def test_markdown_and_html_generate_standalone(query_service: QueryService) -> None:
    report = build_report(query_service)
    markdown = render_markdown(report)
    html = render_html(report)
    assert markdown.startswith("# Project Evolution Report")
    assert html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>")
    assert "<style>" in html  # 内联 CSS，无外部依赖
    # 空节（version）明确标注 V0.1 无 Version 事件
    assert "V0.1 尚无 Version 事件" in markdown
    assert "V0.1 尚无 Version 事件" in html


def test_report_data_only_uses_query_service() -> None:
    """Dashboard 组装层不得 import ledger / sqlite / provider（11 §4）。

    允许 git.evidence.DiffEvidenceStore：Evidence 下钻用其公开目录访问器
    （与 MCP get_evidence 同一路径），不是解析 Git Diff。
    """
    import project_evolution.dashboard.report as report_module

    banned = ("project_evolution.ledger", "sqlite3", "providers", "analyzer")
    with open(report_module.__file__, encoding="utf-8") as fh:
        source = fh.read()
    imported = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and any(b in line for b in banned)
    ]
    assert imported == [], f"dashboard report imports core internals: {imported}"
