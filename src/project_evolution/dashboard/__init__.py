"""Dashboard 与导出（phase-08，11-dashboard-and-export.md）。

- :mod:`report`：Overview / Evolution Report 数据组装（只调用 Query Service）；
- :mod:`export`：Markdown / HTML 渲染（``pel export``，可独立生成，无 Web 依赖）；
- :mod:`app` / :mod:`pages`：FastAPI read-only API + 服务器端渲染页面
  （需安装 ``project-evolution[dashboard]`` extra；默认只监听 loopback）。

Dashboard 不是核心数据源（11 §1）：关闭 / 删除 Dashboard 不影响采集与
Ledger（Core / MCP 完全独立）。
"""

from __future__ import annotations

from typing import Any

from project_evolution.dashboard.export import render_html, render_markdown
from project_evolution.dashboard.report import (
    EvolutionReport,
    Overview,
    build_overview,
    build_report,
    load_evidence_content,
)

_LAZY_APP_EXPORTS = ("create_app", "run_dashboard")


def __getattr__(name: str) -> Any:
    """FastAPI 相关入口惰性导入：不装 dashboard extra 时 export 仍可用。"""
    if name in _LAZY_APP_EXPORTS:
        from project_evolution.dashboard import app as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EvolutionReport",
    "Overview",
    "build_overview",
    "build_report",
    "create_app",
    "load_evidence_content",
    "render_html",
    "render_markdown",
    "run_dashboard",
]
