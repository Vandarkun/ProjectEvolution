"""``python -m project_evolution.mcp serve ...``：MCP stdio Server 入口。

等价于 ``pel mcp serve ...``；参数见 ``project_evolution.cli``。
"""

from __future__ import annotations

import sys

from project_evolution.cli import main

if __name__ == "__main__":
    sys.argv = ["pel", "mcp", *sys.argv[1:]]
    sys.exit(main())
