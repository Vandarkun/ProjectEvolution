"""Dashboard 与导出（phase-08，11-dashboard-and-export.md）。

- :mod:`report`：Overview / Evolution Report 数据组装（只调用 Query Service）；
- :mod:`export`：Markdown / HTML 渲染（``pel export``，可独立生成）；
- :mod:`app`：FastAPI read-only API + 服务器端渲染页面（需安装
  ``project-evolution[dashboard]`` extra；默认只监听 loopback）。

Dashboard 不是核心数据源（11 §1）：关闭 / 删除 Dashboard 不影响采集与
Ledger（Core / MCP 完全独立）。
"""

from project_evolution.dashboard.report import (
    EvolutionReport,
    Overview,
    build_overview,
    build_report,
    load_evidence_content,
)

__all__ = [
    "EvolutionReport",
    "Overview",
    "build_overview",
    "build_report",
    "load_evidence_content",
]
