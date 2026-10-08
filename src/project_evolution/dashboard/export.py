"""Evolution Report 渲染：Markdown 与 HTML 导出（phase-08 §7，11 §6/§7/§8/§9）。

- Markdown：``[EVO-102]`` 形式携带 Event ID（11 §8），单元格文本做管道转义；
- HTML：独立文档（内联 CSS，无外部依赖），所有动态文本经 :func:`esc` 转义
  （11 §9：不执行事件中的 HTML/JS）；
- 两种格式均可独立生成（``pel export``），不依赖 Dashboard 运行。
"""

from __future__ import annotations

from project_evolution.dashboard import htmlutil as H
from project_evolution.dashboard.report import EvolutionReport

_NO_VERSION = "（V0.1 尚无 Version 事件）"


def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def _fmt_time(value: object) -> str:
    text = _fmt(value)
    return text.replace("T", " ").split("+")[0] if text else ""


# ----------------------------------------------------------------- markdown


def _md_cell(value: object) -> str:
    text = _fmt(value).replace("\n", " ").replace("|", "\\|")
    return text if text.strip() else "-"


def _md_arrow(before: object, after: object, *, current: bool = False) -> str:
    text = f"{_md_cell(before)} → {_md_cell(after)}"
    return f"{text} **(CURRENT)**" if current else text


def _md_recent_line(kind: str, title: str, timestamp: str, event_id: str) -> str:
    return f"- 最近{kind}: {title}（{timestamp}）`[{event_id}]`"


def render_markdown(report: EvolutionReport) -> str:
    """Markdown Evolution Report（11 §7 的 10 节结构）。"""
    lines: list[str] = []
    add = lines.append

    add(f"# Project Evolution Report — {report.project_name}")
    add("")
    add(f"- 生成时间: {report.generated_at.isoformat(timespec='seconds')}")
    add(
        f"- 时间范围: {_fmt_time(report.time_start)} ~ {_fmt_time(report.time_end)}"
        if report.time_start
        else "- 时间范围: （暂无事件）"
    )
    add(
        f"- 最新 Version: {report.latest_version}"
        if report.latest_version
        else f"- 最新 Version: {_NO_VERSION}"
    )
    add(
        f"- Projection: version={report.projection.version} "
        f"raw_seq={report.projection.raw_seq} evo_seq={report.projection.evo_seq} "
        f"in_sync={str(report.projection.in_sync).lower()}"
    )
    add("")

    add("## 1. 项目总览")
    add("")
    add(f"- Tasks 总数: {report.task_total}")
    add(f"- 已完成: {report.task_completed}")
    add(f"- Active: {len(report.active_tasks)}")
    for brief in report.active_tasks:
        add(
            f"  - {brief.task_id} {brief.title}（{brief.status}, "
            f"started {_fmt_time(brief.started_at)}）"
        )
    add("")

    add("## 2. 时间范围 / Version")
    add("")
    add(f"- Start: {_fmt_time(report.time_start) or '-'}")
    add(f"- End: {_fmt_time(report.time_end) or '-'}")
    add(f"- Version: {report.latest_version or _NO_VERSION}")
    add("")

    add("## 3. Tasks")
    add("")
    add("| Task | 标题 | 状态 | 开始 | 结束 |")
    add("| --- | --- | --- | --- | --- |")
    for brief in report.tasks:
        add(
            f"| {brief.task_id} | {_md_cell(brief.title)} | {brief.status} "
            f"| {_fmt_time(brief.started_at)} | {_fmt_time(brief.finished_at)} |"
        )
    add("")

    add("## 4. Feature Evolution")
    add("")
    if not report.features:
        add("（无 Feature 变更）")
    for feature in report.features:
        state = feature.current if feature.current is not None else "unknown"
        add(f"### {feature.name} — current: {state}")
        add("")
        add("| 时间 | Action | Before → After | Task | Event |")
        add("| --- | --- | --- | --- | --- |")
        for change in feature.changes:
            add(
                f"| {_fmt_time(change.timestamp)} | {change.action} "
                f"| {_md_arrow(change.before, change.after)} "
                f"| {_md_cell(change.task_id)} | `[{change.event_id}]` |"
            )
        add("")

    add("## 5. Parameter Evolution")
    add("")
    if not report.parameters:
        add("（无 Parameter 变更）")
    for parameter in report.parameters:
        scope = f"（scope: {parameter.scope}）" if parameter.scope else ""
        add(f"### {parameter.name}{scope} — current: {parameter.current or '-'}")
        add("")
        add("| 时间 | Before → After | Unit | Reason | Task | Event |")
        add("| --- | --- | --- | --- | --- | --- |")
        last_index = len(parameter.changes) - 1
        for index, item in enumerate(parameter.changes):
            add(
                f"| {_fmt_time(item.timestamp)} "
                f"| {_md_arrow(item.before, item.after, current=index == last_index)} "
                f"| {_md_cell(item.unit)} | {_md_cell(item.reason)} "
                f"| {_md_cell(item.task_id)} | `[{item.event_id}]` |"
            )
        add("")

    add("## 6. Decision History")
    add("")
    if not report.decisions:
        add("（无 Decision）")
    for decision in report.decisions:
        current_text = (
            f"current: {decision.current.decision} `[{decision.current.event_id}]`"
            if decision.current
            else "current: -"
        )
        add(f"### {decision.subject} — {current_text}")
        add("")
        for entry in decision.decisions:
            status = "active" if entry.active else "superseded"
            add(
                f"- [{status}] {entry.decision}（{_fmt_time(entry.timestamp)}）"
                f" `[{entry.event_id}]`"
            )
            if entry.supersedes:
                add(f"  - supersedes: `[{', '.join(entry.supersedes)}]`")
            for superseder in entry.superseded_by:
                add(f"  - superseded by: `[{superseder}]`")
        add("")

    add("## 7. Bug / Fix History")
    add("")
    if not report.bugs:
        add("（无 BugFix）")
    else:
        add("| 时间 | Problem | Fix | Scope | Verification | Event |")
        add("| --- | --- | --- | --- | --- | --- |")
        for bug in report.bugs:
            add(
                f"| {_fmt_time(bug.timestamp)} | {_md_cell(bug.problem)} "
                f"| {_md_cell(bug.fix)} | {_md_cell(', '.join(bug.affected_scope))} "
                f"| {_md_cell(bug.verification)} | `[{bug.event_id}]` |"
            )
    add("")

    add("## 8. 关键 Changed Files")
    add("")
    if not report.files:
        add("（无文件变更）")
    else:
        add("| 文件 | 变更次数 | First | Last |")
        add("| --- | --- | --- | --- |")
        for summary in report.files:
            add(
                f"| {summary.path} | {summary.change_count} "
                f"| {_fmt_time(summary.first_at)} | {_fmt_time(summary.last_at)} |"
            )
    add("")

    add("## 9. 当前状态")
    add("")
    add(f"- Active Features: {', '.join(report.current_state.active_features) or '-'}")
    add("- Current Parameters:")
    for name, value in report.current_state.current_parameters.items():
        add(f"  - {name} = {value}")
    add(f"- Active Decisions: {', '.join(report.current_state.active_decisions) or '-'}")
    add("- Recent Bug Fixes:")
    for problem in report.current_state.recent_bug_fixes:
        add(f"  - {problem}")
    add("")

    add("## 10. Evidence / Provenance 摘要")
    add("")
    add(f"- Raw Ledger 事件: {report.provenance.raw_events}")
    add(f"- Evolution Ledger 事件: {report.provenance.evolution_events}")
    add(f"- Evidence 记录: {report.provenance.evidence_records}")
    add(
        f"- 被引用 Evidence: {len(report.provenance.referenced_evidence_ids)}"
    )
    add("- 报告中的 `[EVO-*]` / `[RAW-*]` 均为底层 Event ID，可在账本中回溯。")
    add("")
    return "\n".join(lines)


# --------------------------------------------------------------------- html

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
       padding: 0 1rem; color: #1f2430; }
h1, h2 { border-bottom: 1px solid #d8dce6; padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; }
th, td { border: 1px solid #d8dce6; padding: .3rem .5rem; text-align: left;
         vertical-align: top; }
th { background: #f2f4f8; }
code { background: #f2f4f8; padding: 0 .25rem; }
details { margin: .25rem 0; } summary { cursor: pointer; }
footer { color: #6b7280; font-size: .85rem; margin-top: 2rem; }
"""


def render_html(report: EvolutionReport) -> str:
    """独立 HTML Evolution Report：全部文本转义，Event ID 以纯文本标注。"""
    parts: list[str] = []
    add = parts.append

    add("<!DOCTYPE html>")
    add(
        f'<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>Evolution Report — {H.esc(report.project_name)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
    )
    add(f"<h1>Evolution Report — {H.esc(report.project_name)}</h1>")
    add(H.ul([
        f"生成时间: {_fmt(report.generated_at.isoformat(timespec='seconds'))}",
        f"时间范围: {_fmt_time(report.time_start)} ~ {_fmt_time(report.time_end)}",
        f"最新 Version: {report.latest_version or _NO_VERSION}",
        f"Projection: version={report.projection.version} "
        f"in_sync={report.projection.in_sync}",
    ]))

    add("<h2>1. 项目总览</h2>")
    add(H.ul([
        f"Tasks 总数: {report.task_total}",
        f"已完成: {report.task_completed}",
        f"Active: {len(report.active_tasks)}",
    ]))

    add("<h2>2. 时间范围 / Version</h2>")
    add(H.table(
        ["Start", "End", "Version"],
        [[
            _fmt_time(report.time_start),
            _fmt_time(report.time_end),
            report.latest_version or _NO_VERSION,
        ]],
    ))

    add("<h2>3. Tasks</h2>")
    add(H.table(
        ["Task", "标题", "状态", "开始", "结束"],
        (
            [b.task_id, b.title, b.status, _fmt_time(b.started_at), _fmt_time(b.finished_at)]
            for b in report.tasks
        ),
    ))

    add("<h2>4. Feature Evolution</h2>")
    for feature in report.features:
        state = feature.current if feature.current is not None else "unknown"
        add(H.tag("h3", H.esc(f"{feature.name} — current: {state}")))
        add(H.table(
            ["时间", "Action", "Before → After", "Task", "Event"],
            (
                [c.timestamp, c.action, f"{c.before or '-'} → {c.after or '-'}",
                 c.task_id or "-", c.event_id]
                for c in feature.changes
            ),
        ))

    add("<h2>5. Parameter Evolution</h2>")
    for parameter in report.parameters:
        scope = f"（scope: {parameter.scope}）" if parameter.scope else ""
        add(
            H.tag(
                "h3",
                H.esc(f"{parameter.name}{scope} — current: {parameter.current or '-'}"),
            )
        )
        add(H.table(
            ["时间", "Before → After", "Unit", "Reason", "Task", "Event"],
            (
                [c.timestamp, f"{c.before or '-'} → {c.after or '-'}", c.unit or "-",
                 c.reason or "-", c.task_id or "-", c.event_id]
                for c in parameter.changes
            ),
        ))

    add("<h2>6. Decision History</h2>")
    for decision in report.decisions:
        current = decision.current
        current_text = (
            f"current: {current.decision} [{current.event_id}]" if current else "current: -"
        )
        add(H.tag("h3", H.esc(f"{decision.subject} — {current_text}")))
        add(H.table(
            ["时间", "Decision", "状态", "supersedes", "superseded by", "Event"],
            (
                [d.timestamp, d.decision, "active" if d.active else "superseded",
                 ", ".join(d.supersedes) or "-", ", ".join(d.superseded_by) or "-",
                 d.event_id]
                for d in decision.decisions
            ),
        ))

    add("<h2>7. Bug / Fix History</h2>")
    add(H.table(
        ["时间", "Problem", "Fix", "Scope", "Verification", "Event"],
        (
            [b.timestamp, b.problem, b.fix, ", ".join(b.affected_scope),
             b.verification or "-", b.event_id]
            for b in report.bugs
        ),
    ))

    add("<h2>8. 关键 Changed Files</h2>")
    add(H.table(
        ["文件", "变更次数", "First", "Last"],
        ([f.path, f.change_count, _fmt_time(f.first_at), _fmt_time(f.last_at)]
         for f in report.files),
    ))

    add("<h2>9. 当前状态</h2>")
    add(H.tag("h3", "Active Features"))
    add(H.ul(report.current_state.active_features or ["-"]))
    add(H.tag("h3", "Current Parameters"))
    add(H.ul(
        [f"{name} = {value}" for name, value in
         report.current_state.current_parameters.items()] or ["-"]
    ))
    add(H.tag("h3", "Active Decisions"))
    add(H.ul(report.current_state.active_decisions or ["-"]))

    add("<h2>10. Evidence / Provenance 摘要</h2>")
    add(H.ul([
        f"Raw Ledger 事件: {report.provenance.raw_events}",
        f"Evolution Ledger 事件: {report.provenance.evolution_events}",
        f"Evidence 记录: {report.provenance.evidence_records}",
        f"被引用 Evidence: {len(report.provenance.referenced_evidence_ids)}",
        "报告中的 [EVO-*] / [RAW-*] 均为底层 Event ID，可在账本中回溯。",
    ]))

    add(
        f"<footer>Project Evolution Ledger — generated by pel export, "
        f"project root: {H.esc(report.project_root)}</footer>"
    )
    add("</body></html>")
    return "\n".join(parts)
