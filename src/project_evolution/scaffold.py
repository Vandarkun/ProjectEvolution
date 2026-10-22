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
- dsh       DeepSeek Harness（deepseek-ai/deepseek-harness）：用户级
            ``$DSH_HOME/cordis.yml``（默认 ``~/.dsh``）追加
            ``@deepseek-ai/dsh-mcp-client`` 插件条目；项目 AGENTS.md 生态原生
- deepcode  deepcode-cli（另一个工具，勿与 dsh 混淆）：用户级
            ``~/.deepcode/settings.json`` → ``mcpServers``
"""

from __future__ import annotations

import json
import os
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


def _dsh_home() -> Path:
    """dsh harness 主目录：$DSH_HOME 优先，默认 ~/.dsh（官方 config-catalog）。"""
    env = os.environ.get("DSH_HOME")
    return Path(env) if env else Path.home() / ".dsh"


def _upsert_dsh_plugin(config_text: str, entry_lines: list[str]) -> str:
    """在 cordis.yml 中幂等插入/替换 pel 插件条目（保留其余内容）。

    dsh 处于 developer preview，官方未承诺稳定的跨版本配置文件格式，
    这里做保守的文本级 upsert：已有 ``id: pel`` 块则整块替换；否则挂到
    ``plugins:`` 键下（无该键则补一个）。
    """
    lines = config_text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("- id:") and "pel" in stripped.split("id:", 1)[1].strip():
            # 跳过旧 pel 块：直到下一个顶层键或同级列表项
            i += 1
            while i < len(lines):
                nxt = lines[i]
                s = nxt.strip()
                if s.startswith("- ") or (nxt and not nxt[0].isspace()):
                    break
                i += 1
            out.extend(entry_lines)
            replaced = True
            continue
        out.append(line)
        i += 1
    text = "\n".join(out)
    if replaced:
        return text.rstrip("\n") + "\n" if text else ""

    indented = [f"  {line}" for line in entry_lines]
    if any(line.strip() == "plugins:" for line in lines):
        # 挂到现有 plugins: 键末尾（该键的最后一个子项之后）
        result: list[str] = []
        idx = next(
            k for k, line in enumerate(lines) if line.strip() == "plugins:"
        )
        j = idx + 1
        while j < len(lines) and (not lines[j] or lines[j][0].isspace()):
            j += 1
        result = lines[:j] + indented + lines[j:]
        return "\n".join(result).rstrip("\n") + "\n"
    if lines and lines[0].startswith("- "):
        # 整个文件就是一个插件列表
        return "\n".join(lines + entry_lines).rstrip("\n") + "\n"
    if not text.strip():
        return "plugins:\n" + "\n".join(indented) + "\n"
    return text.rstrip("\n") + "\n\nplugins:\n" + "\n".join(indented) + "\n"


def init_dsh(project: Path, server_command: str | None) -> ScaffoldResult:
    """dsh（DeepSeek Harness）：~/.dsh/cordis.yml 追加 dsh-mcp-client 插件条目。"""
    _require_uv()
    result = ScaffoldResult("dsh")
    cmd = _server_command(None, server_command)  # 全局条目：cwd 自动解析项目
    config = _dsh_home() / "cordis.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    args_yaml = ", ".join(json.dumps(a) for a in cmd[1:])
    entry_lines = [
        "- id: pel",
        "  name: '@deepseek-ai/dsh-mcp-client'",
        "  config:",
        "    serverName: pel",
        "    transport: stdio",
        f"    command: {json.dumps(cmd[0])}",
        f"    args: [{args_yaml}]",
    ]
    old = config.read_text(encoding="utf-8") if config.is_file() else ""
    new = _upsert_dsh_plugin(old, entry_lines)
    if new != old:
        config.write_text(new, encoding="utf-8")
        result.written.append(str(config))
    else:
        result.skipped.append(f"{config} (unchanged)")
    if _write_agents_md(project):
        result.written.append("AGENTS.md")
    else:
        result.skipped.append("AGENTS.md (already has PEL rules)")
    result.notes.append(
        "dsh 为 developer preview，配置格式可能随版本变化；工具将以 "
        "mcp__pel__* 形式出现；按工作区隔离可用官方 dsh-workspace-mcp 插件"
    )
    return result


HARNESSES = {
    "zcode": init_zcode,
    "codex": init_codex,
    "deepcode": init_deepcode,
    "dsh": init_dsh,
    "deepseek-harness": init_dsh,
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
