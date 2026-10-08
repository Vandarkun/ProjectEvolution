"""Dashboard 页面（phase-08 §6，11-dashboard-and-export.md §2/§9）。

前端方案取"开发量最小且清晰"：无 JS / 无前端构建，FastAPI 服务器端渲染
纯 HTML + CSS。页面数据全部由 ``report.py`` 从 Query Service 组装
（phase-08 §5：禁止前端直读 JSONL / SQLite / 解析 Diff）。

转义规则（与 :mod:`htmlutil` 配合）：

- 纯文本一律走 ``H.para`` / ``H.table`` / ``H.ul`` 等自动转义函数；
- 链接等组合 HTML 通过 ``H.tag`` 的 raw 子内容拼接，子内容本身由
  ``H.esc`` / 安全构建函数产生 —— 页面没有任何直接拼接用户文本的出口。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from urllib.parse import quote, urlencode

from project_evolution.dashboard import htmlutil as H
from project_evolution.dashboard.report import Overview
from project_evolution.index import (
    EvidenceRecord,
    ParameterHistory,
    TaskHistory,
    TimelineItem,
)

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; color: #1f2430;
       background: #fafbfc; }
nav { background: #1f2430; padding: .6rem 1rem; display: flex; gap: 1.25rem;
      align-items: center; }
nav a { color: #fff; text-decoration: none; font-size: .95rem; }
nav a.active { border-bottom: 2px solid #7aa2f7; }
nav .brand { font-weight: 600; color: #7aa2f7; margin-right: .5rem; }
main { max-width: 64rem; margin: 1.5rem auto; padding: 0 1rem; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 1.5rem; }
h3 { font-size: 1rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem;
        background: #fff; }
th, td { border: 1px solid #d8dce6; padding: .3rem .5rem; text-align: left;
         vertical-align: top; font-size: .9rem; }
th { background: #f2f4f8; }
code { background: #f2f4f8; padding: 0 .2rem; font-size: .85rem; }
.item { display: flex; gap: .5rem; align-items: flex-start; margin: .4rem 0; }
.badge { flex: none; padding: 0 .4rem; border-radius: .6rem; font-size: .78rem;
         background: #e7ecf5; height: fit-content; }
.badge.task { background: #dbeafe; } .badge.checkpoint { background: #ede9fe; }
.badge.feature { background: #dcfce7; } .badge.parameter { background: #fef9c3; }
.badge.decision { background: #fce7f3; } .badge.bug { background: #fee2e2; }
.badge.version { background: #e0f2fe; }
details { flex: 1; background: #fff; border: 1px solid #d8dce6;
          border-radius: .25rem; padding: .3rem .5rem; }
summary { cursor: pointer; }
pre { background: #fff; border: 1px solid #d8dce6; padding: .5rem;
      overflow-x: auto; font-size: .8rem; white-space: pre-wrap; }
form.inline { display: flex; gap: .6rem; align-items: center; margin: .5rem 0;
              flex-wrap: wrap; }
input[type=text] { padding: .25rem .4rem; border: 1px solid #c4cad4;
                   border-radius: .2rem; }
button { padding: .25rem .7rem; }
.muted { color: #6b7280; font-size: .85rem; }
footer { color: #6b7280; font-size: .8rem; margin: 2rem 0; }
"""

_KIND_LABELS = {
    "task": "Task",
    "checkpoint": "Checkpoint",
    "feature": "Feature",
    "parameter": "Parameter",
    "decision": "Decision",
    "bug": "Bug",
    "version": "Version",
}


def _time(value: object) -> str:
    return str(value).replace("T", " ").split("+")[0] if value else "-"


def _link(href: str, text: object) -> str:
    return f'<a href="{H.esc(href)}">{H.esc(text)}</a>'


def _task_link(task_id: str) -> str:
    return _link(f"/tasks/{quote(task_id, safe='')}", task_id)


def page_shell(title: str, *body: str, active: str) -> str:
    """HTML 文档骨架 + 导航（active: overview / timeline）。"""
    nav_items = []
    for key, href, label in (
        ("overview", "/", "Overview"),
        ("timeline", "/timeline", "Timeline"),
    ):
        class_attr = ' class="active"' if key == active else ""
        nav_items.append(f'<a href="{href}"{class_attr}>{label}</a>')
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{H.esc(title)} — PEL Dashboard</title>"
        f"<style>{_PAGE_CSS}</style></head><body>",
        f'<nav><span class="brand">PEL</span>{"".join(nav_items)}</nav>',
        "<main>", *body,
        "<footer>Project Evolution Ledger — read-only dashboard; "
        "all data comes from the Query Service.</footer>",
        "</main></body></html>",
    ])


# ----------------------------------------------------------------- overview


def overview_page(overview: Overview) -> str:
    body: list[str] = [f"<h1>{H.esc(overview.project_name)}</h1>"]
    body.append(H.para(
        f"最新 Version: {overview.latest_version or '（V0.1 尚无 Version 事件）'}"
        f" ｜ 时间范围: {_time(overview.time_start)} ~ {_time(overview.time_end)}"
        f" ｜ Projection v{overview.projection.version}"
        f"{' (in sync)' if overview.projection.in_sync else ' (stale)'}"
    ))

    body.append(H.tag("h2", "任务摘要"))
    body.append(H.table(
        ["Tasks 总数", "已完成", "Active"],
        [[overview.task_total, overview.task_completed, len(overview.active_tasks)]],
    ))
    if overview.active_tasks:
        body.append(H.tag("h3", "Active Tasks"))
        body.append(H.tag("ul", *(
            H.tag("li", f"{_task_link(brief.task_id)} — {H.esc(brief.title)}"
                        f"（{H.esc(brief.status)}）")
            for brief in overview.active_tasks
        )))

    body.append(H.tag("h2", "最近变化"))
    recent_rows = []
    for kind in ("feature", "parameter", "bug", "decision"):
        item = overview.recent.get(kind)
        recent_rows.append([
            _KIND_LABELS.get(kind, kind),
            item.title if item else "-",
            _time(item.timestamp) if item else "-",
            f"[{item.event_id}]" if item else "-",
        ])
    body.append(H.table(["类型", "标题", "时间", "Event"], recent_rows))

    body.append(H.tag("h2", "当前状态"))
    state = overview.current_state
    body.append(H.tag("h3", "Current Parameters"))
    parameter_items = [
        H.tag("li", _link(f"/parameters?{urlencode({'name': name})}",
                          f"{name} = {value}"))
        for name, value in state.current_parameters.items()
    ]
    body.append(H.tag("ul", *(parameter_items or [H.esc("-")])))
    body.append(H.tag("h3", "Active Features"))
    body.append(H.ul(state.active_features or ["-"]))
    body.append(H.tag("h3", "Active Decisions"))
    body.append(H.ul(state.active_decisions or ["-"]))

    body.append(
        '<form class="inline" action="/parameters" method="get">'
        "<label>Parameter History：</label>"
        '<input type="text" name="name" placeholder="如 OpacityMin" required>'
        '<button type="submit">查看</button></form>'
    )
    return page_shell("Overview", *body, active="overview")


# ----------------------------------------------------------------- timeline


def _timeline_item(item: TimelineItem) -> str:
    badge = (
        f'<span class="badge {H.esc(item.kind)}">'
        f"{H.esc(_KIND_LABELS.get(item.kind, item.kind))}</span>"
    )
    evidence = (
        f' <span class="muted">evidence ×{item.evidence_count}</span>'
        if item.evidence_count
        else ""
    )
    stamp = f"{_time(item.timestamp)}{evidence}"
    summary_head = f'{H.esc(item.title)} <span class="muted">{stamp}</span>'
    if item.kind == "checkpoint":
        inner = H.para(item.summary or "-")
    else:
        parts = [H.para(item.summary or "-")]
        if item.task_id:
            parts.append(H.tag("p", f"Task: {_task_link(item.task_id)}"))
        inner = "".join(parts)
    return (
        f'<div class="item">{badge}'
        f"<details><summary>{summary_head}</summary>{inner}</details></div>"
    )


def timeline_page(items: Sequence[TimelineItem], selected: Iterable[str]) -> str:
    """Timeline 页：kind 复选框（GET 表单筛选）+ 可折叠条目。"""
    selected_set = set(selected)
    checkboxes = " ".join(
        f'<label><input type="checkbox" name="kind" value="{H.esc(kind)}"'
        f'{" checked" if kind in selected_set else ""}>{H.esc(label)}</label>'
        for kind, label in _KIND_LABELS.items()
    )
    body: list[str] = [
        H.tag("h1", "Timeline"),
        f'<form class="inline" action="/timeline" method="get">{checkboxes}'
        '<button type="submit">筛选</button></form>',
        H.para(
            f"{len(items)} 条（按时间正序；File Event 不进入 Timeline，"
            "在 Task Detail 中展开）", **{"class": "muted"},
        ),
        *(_timeline_item(item) for item in items),
    ]
    if not items:
        body.append(H.para("（无匹配条目）"))
    return page_shell("Timeline", *body, active="timeline")


# -------------------------------------------------------------- task detail


def task_detail_page(task: TaskHistory) -> str:
    body: list[str] = [f"<h1>{H.esc(task.task_id)} — {H.esc(task.title)}</h1>"]
    body.append(H.table(
        ["状态", "开始", "结束", "Sessions", "文件变更数", "测试通过", "测试失败"],
        [[
            task.status, _time(task.started_at), _time(task.finished_at),
            ", ".join(task.session_ids) or "-", task.file_change_count,
            task.tests_passed, task.tests_failed,
        ]],
    ))

    body.append(H.tag("h2", "Checkpoints"))
    if task.checkpoints:
        for checkpoint in task.checkpoints:
            summary = checkpoint.summary or f"Checkpoint #{checkpoint.index}"
            rows = H.table(
                ["index", "base_ref", "head_ref", "diff evidence"],
                [[
                    checkpoint.index, checkpoint.base_ref or "-",
                    checkpoint.head_ref or "-",
                    checkpoint.evidence_id or "-",
                ]],
            )
            evidence_link = (
                _link(f"/evidence/{quote(checkpoint.evidence_id, safe='')}",
                      checkpoint.evidence_id)
                if checkpoint.evidence_id
                else H.esc("-")
            )
            inner = f"<p>Summary: {H.esc(summary)}</p>{rows}<p>Evidence: {evidence_link}</p>"
            head = (
                f"#{checkpoint.index} {H.esc(summary)} "
                f"<span class=\"muted\">{_time(checkpoint.created_at)}</span>"
            )
            body.append(f"<details><summary>{head}</summary>{inner}</details>")
    else:
        body.append(H.para("（无 Checkpoint）"))

    body.append(H.tag("h2", "Changed Files"))
    body.append(H.ul(task.files or ["-"]))

    body.append(H.tag("h2", "Evolution Events"))
    if task.evolution:
        table_rows = [
            [
                item.timestamp, item.type, item.title, item.detail or "-",
                item.task_id or "-",
                ", ".join(item.evidence_ids) or "-",
            ]
            for item in task.evolution
        ]
        body.append(H.table(
            ["时间", "类型", "标题", "详情", "Task", "Evidence"], table_rows,
        ))
        evidence_links = sorted({
            eid for item in task.evolution for eid in item.evidence_ids
        })
        body.append(H.tag("h3", "Evidence 入口"))
        if evidence_links:
            body.append(H.tag("ul", *(
                H.tag("li", _link(f"/evidence/{quote(eid, safe='')}", eid))
                for eid in evidence_links
            )))
        else:
            body.append(H.para("（无 Evidence）"))
    else:
        body.append(H.para("（无语义事件）"))

    return page_shell(f"Task {task.task_id}", *body, active="overview")


# -------------------------------------------------------- parameter history


def parameter_page(history: ParameterHistory) -> str:
    """Parameter History：Before → After → Current（phase-08 §6）。"""
    scope_note = f"（scope: {history.scope}）" if history.scope else ""
    body: list[str] = [
        f"<h1>{H.esc(history.name)}{H.esc(scope_note)}</h1>",
        H.para(f"current = {history.current if history.current is not None else '-'}"),
    ]
    if not history.scope and history.current_by_scope:
        body.append(H.para(
            "按 scope 拆分："
            + ", ".join(
                f"{scope or 'global'} = {value}"
                for scope, value in history.current_by_scope.items()
            )
        ))
    rows = []
    last_index = len(history.changes) - 1
    for index, change in enumerate(history.changes):
        arrow = f"{change.before if change.before is not None else 'null'} → {change.after}"
        if index == last_index and history.current is not None:
            arrow += "  (CURRENT)"
        rows.append([
            _time(change.timestamp), arrow, change.unit or "-",
            change.reason or "-", change.task_id or "-",
            f"[{change.event_id}]",
        ])
    body.append(H.table(["时间", "Before → After", "Unit", "Reason", "Task", "Event"], rows))
    return page_shell(f"Parameter {history.name}", *body, active="overview")


# ----------------------------------------------------------------- evidence


def evidence_page(record: EvidenceRecord, content: str | None, *, truncated: bool) -> str:
    body: list[str] = [f"<h1>Evidence — {H.esc(record.evidence_id)}</h1>"]
    body.append(H.table(
        ["类型", "创建时间", "Task", "Checkpoint", "ref"],
        [[record.type, _time(record.created_at), record.task_id or "-",
          record.checkpoint_id or "-", record.ref or "-"]],
    ))
    if content is None:
        body.append(H.para("（该 Evidence 无 Patch 内容，或内容文件不存在）"))
    else:
        note = "（超过 20,000 字符已截断）" if truncated else ""
        body.append(H.tag("h2", f"Patch（纯文本）{H.esc(note)}"))
        body.append(H.pre(content))
    return page_shell(f"Evidence {record.evidence_id}", *body, active="overview")
