"""Agent Instruction Sample（phase-07 §8，08-mcp-integration.md §10）。

最短项目规则：指导 Agent 何时调用哪个工具。规则刻意保持短（本身不能
成为高 Token 开销），且明确"不要每轮都调用工具"。
"""

from __future__ import annotations

AGENT_INSTRUCTIONS = """\
This project uses the Project Evolution Ledger (PEL) MCP tools.

- Call `start_task` when you begin a well-defined development task
  (feature, bugfix, refactor).
- Call `checkpoint_task` when a significant subgoal is done, an important
  design choice was made, key tests first pass, or you are about to
  compact/switch context.
- Call `complete_task` when the task is finished.
- Call `get_history` / `get_project_state` when you need past designs,
  parameters, decisions, or bug fixes.
- Call `search_events` when you don't know the exact parameter/decision name —
  it matches by meaning across Chinese and English.
- Never edit files under `.project-evolution/events` by hand.

Do not call these tools every turn — only at the moments above.
"""


def render_agent_instructions(*, project: str | None = None) -> str:
    """输出 AGENTS.md 片段；``project`` 给定时在多项目服务器下补充解析提示。"""
    if project is None:
        return AGENT_INSTRUCTIONS
    return (
        AGENT_INSTRUCTIONS
        + f"\nThis server serves multiple projects; pass `project={project}` in every tool call.\n"
    )
