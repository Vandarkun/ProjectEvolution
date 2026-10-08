"""MCP 集成层（phase-07）：stdio Server、五个核心工具与 Project 解析。

MCP 层只做参数解析 / Project 解析 / Core 调用 / 结构化结果 / 协议安全，
不实现任何 Git、Evolution、Ledger 业务逻辑（08-mcp-integration.md §1）。
"""

from project_evolution.mcp.errors import ToolError
from project_evolution.mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS
from project_evolution.mcp.registry import ProjectEntry, ProjectRegistry
from project_evolution.mcp.server import PelMcpServer, configure_logging
from project_evolution.mcp.service import PelToolService, ProviderSpec, build_provider

__all__ = [
    "PelMcpServer",
    "PelToolService",
    "ProjectEntry",
    "ProjectRegistry",
    "ProviderSpec",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "ToolError",
    "build_provider",
    "configure_logging",
]
