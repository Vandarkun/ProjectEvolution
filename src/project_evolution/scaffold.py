"""``pel init`` 脚手架：把 PEL 一行命令接入各 Coding Agent harness。

设计（适配层只做"搬运"，不动核心）：

- 每个 harness 一个模板条目：往它的 MCP 配置里写入 pel 服务器注册
  （幂等合并，绝不覆盖用户已有配置），并在项目根放/补 AGENTS.md 记录规则；
- MCP 服务器命令默认用 ``uvx --from git+...`` 形式——换机器不依赖本地路径
  （可用 ``--server-command`` 覆盖为本地 venv 的 pel.exe）；
- workspace 级配置（zcode）写显式 ``--project`` 绝对路径；用户级全局配置
  （codex 的 config.toml、deepcode 的 settings.json）不写 ``--project``，
  由服务器按 cwd 自动解析（各 harness 以项目目录为工作目录启动服务器）。

harness 扩展机制参考：

- zcode     workspace ``.zcode/config.json`` → ``mcp.servers``（自动连接）
- codex     用户级 ``~/.codex/config.toml`` → ``[mcp_servers.pel]``
- deepcode  用户级 ``~/.deepcode/settings.json`` → ``mcpServers``
            （Deep Code / ds harness；同时读项目 AGENTS.md 与 .agents/skills）
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from project_evolution.errors import PelError

#: uvx 直装源（不克隆仓库的一行命令体验）
GIT_SOURCE = "git+https://github.com/Vandarkun/ProjectEvolution"

AGENTS_MD_MARKER = "<!-- PEL:BEGIN -->"
AGENTS_MD_SECTION = """\
<!-- PEL:BEGIN -->
## PEL 工程演进记录（自动生成，可整段删除）

- 开始一个明确开发任务时调用 `start_task`；完成子阶段 / 关键设计取舍 /
  关键测试首次通过 / 切换上下文前调用 `checkpoint_task`（摘要只写事实）；
  任务结束调用 `complete_task`。
- 改设计前先查：`get_project_state`（当前参数/决策）、`search_events`
  （语义检索，中英跨语言）、`get_history`（精确历史）、`get_evidence`（证据）。
- CLI 等价入口：`pel task start/checkpoint/complete`、`pel search`。
- 永远不要手改 `.project-evolution/events/` 下的文件。
- 不要每轮都调用这些工具，只在上述节点调用。
<!-- PEL:END -->
"""


class ScaffoldError(PelError):
    """脚手架失败（未知 harness / 文件不可写）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.kind = "scaffold_error"


@dataclass
class ScaffoldResult:
    harness: str
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- helpers


def _server_command(project: Path | None, server_command: str | None) -> list[str]:
    """MCP 服务器命令：默认 uvx 直装源；project 给定时追加显式 --project。"""
    if server_command:
        parts = server_command.split()
    else:
        parts = ["uvx", "--from", GIT_SOURCE, "pel", "mcp", "serve"]
    if project is not None and "--project" not in parts:
        parts += ["--project", str(project.resolve())]
    return parts


def _merge_json(path: Path, mutate) -> bool:
    """合并式写 JSON：已有文件保留其余键；返回是否有变化。"""
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ScaffoldError(f"cannot parse existing config {path}: {exc}") from exc
    before = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    changed = mutate(data) or False
    after = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if after != before or changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after + "\n", encoding="utf-8")
        return True
    return False


def _write_agents_md(project: Path) -> bool:
    """项目 AGENTS.md：无则创建，有则只在缺少标记段时追加一次。"""
    path = project / "AGENTS.md"
    if not path.is_file():
        path.write_text("# 项目开发规则\n\n" + AGENTS_MD_SECTION, encoding="utf-8")
        return True
    text = path.read_text(encoding="utf-8")
    if AGENTS_MD_MARKER in text:
        return False
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n" + AGENTS_MD_SECTION, encoding="utf-8")
    return True


def _require_uv() -> None:
    if shutil.which("uv") is None and shutil.which("uvx") is None:
        raise ScaffoldError(
            "uv/uvx not found; install it first: pip install uv "
            "(or pass --server-command with a local pel command)"
        )


# ----------------------------------------------------------------- harness


def init_zcode(project: Path, server_command: str | None) -> ScaffoldResult:
    """zcode：workspace .zcode/config.json → mcp.servers（打开即自动连接）。"""
    _require_uv()
    result = ScaffoldResult("zcode")
    cmd = _server_command(project, server_command)

    def mutate(data: dict) -> bool:
        servers = data.setdefault("mcp", {}).setdefault("servers", {})
        entry = {"command": cmd[0], "args": cmd[1:]}
        if servers.get("pel") == entry:
            return False
        servers["pel"] = entry
        return True

    if _merge_json(project / ".zcode" / "config.json", mutate):
        result.written.append(".zcode/config.json")
    else:
        result.skipped.append(".zcode/config.json (unchanged)")
    if _write_agents_md(project):
        result.written.append("AGENTS.md")
    else:
        result.skipped.append("AGENTS.md (already has PEL rules)")
    result.notes.append("重启 ZCode 会话后 MCP 自动连接")
    return result


def init_codex(project: Path, server_command: str | None) -> ScaffoldResult:
    """codex：用户级 ~/.codex/config.toml → [mcp_servers.pel]（TOML 段幂等替换）。"""
    _require_uv()
    result = ScaffoldResult("codex")
    cmd = _server_command(None, server_command)  # 全局配置：cwd 自动解析项目
    header = "[mcp_servers.pel]"
    lines = [header] + [
        f'command = "{cmd[0]}"',
        f"args = {json.dumps(cmd[1:])}",
    ]
    section = "\n".join(lines)

    config = Path.home() / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("[") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    kept = [
        b for b in blocks if not b.strip().splitlines()[0].strip().startswith(header)
    ]
    kept.append(section)
    new_text = "\n\n".join(b for b in kept if b.strip()) + "\n"
    if new_text != text:
        config.write_text(new_text, encoding="utf-8")
        result.written.append(str(config))
    else:
        result.skipped.append(f"{config} (unchanged)")
    if _write_agents_md(project):
        result.written.append("AGENTS.md")
    else:
        result.skipped.append("AGENTS.md (already has PEL rules)")
    result.notes.append("Codex 原生读取项目 AGENTS.md；多项目共用此全局服务器")
    return result


def init_deepcode(project: Path, server_command: str | None) -> ScaffoldResult:
    """deepcode（ds harness）：~/.deepcode/settings.json → mcpServers 合并。"""
    _require_uv()
    result = ScaffoldResult("deepcode")
    cmd = _server_command(None, server_command)  # 全局配置：cwd 自动解析项目

    def mutate(data: dict) -> bool:
        servers = data.setdefault("mcpServers", {})
        entry = {"command": cmd[0], "args": cmd[1:]}
        if servers.get("pel") == entry:
            return False
        servers["pel"] = entry
        return True

    config = Path.home() / ".deepcode" / "settings.json"
    if _merge_json(config, mutate):
        result.written.append(str(config))
    else:
        result.skipped.append(f"{config} (unchanged)")
    if _write_agents_md(project):
        result.written.append("AGENTS.md")
    else:
        result.skipped.append("AGENTS.md (already has PEL rules)")
    result.notes.append("Deep Code 用 /mcp 查看连接状态；原生读项目 AGENTS.md")
    return result


HARNESSES = {
    "zcode": init_zcode,
    "codex": init_codex,
    "deepcode": init_deepcode,
}


def run_init(harness: str, project: Path, server_command: str | None) -> ScaffoldResult:
    """执行适配：未知 harness 明确报错。"""
    try:
        handler = HARNESSES[harness]
    except KeyError:
        raise ScaffoldError(
            f"unknown harness {harness!r}; supported: {', '.join(sorted(HARNESSES))}"
        ) from None
    if not project.is_dir():
        raise ScaffoldError(f"project directory not found: {project}")
    return handler(project, server_command)
