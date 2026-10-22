"""pel init 脚手架：幂等合并、不覆盖既有配置、AGENTS.md 只追加一次。

用户级配置（codex/deepcode）通过 monkeypatch Path.home 隔离到 tmp_path。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_evolution.scaffold import (
    GIT_SOURCE,
    ScaffoldError,
    run_init,
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake)
    return fake


@pytest.fixture()
def with_uv(monkeypatch):
    monkeypatch.setattr("project_evolution.scaffold._require_uv", lambda: None)


def _uvx_cmd(project: Path | None) -> dict:
    args = ["--from", GIT_SOURCE, "pel", "mcp", "serve"]
    if project is not None:
        args += ["--project", str(project.resolve())]
    return {"command": "uvx", "args": args}


def test_zcode_workspace_config(home: Path, tmp_path: Path, with_uv) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    run_init("zcode", project, None)

    cfg = json.loads((project / ".zcode" / "config.json").read_text(encoding="utf-8"))
    assert cfg["mcp"]["servers"]["pel"] == _uvx_cmd(project)
    assert (project / "AGENTS.md").read_text(encoding="utf-8").count("PEL:BEGIN") == 1

    # 幂等：重跑无变更
    again = run_init("zcode", project, None)
    assert not again.written and len(again.skipped) == 2

    # 合并：不覆盖用户已有键
    (project / ".zcode" / "config.json").write_text(
        json.dumps({"other": {"keep": True}}), encoding="utf-8"
    )
    run_init("zcode", project, None)
    cfg = json.loads((project / ".zcode" / "config.json").read_text(encoding="utf-8"))
    assert cfg["other"] == {"keep": True} and "pel" in cfg["mcp"]["servers"]


def test_codex_global_toml(home: Path, tmp_path: Path, with_uv) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.other]\ncommand = "keep-me"\n\n[model]\nname = "deepseek-chat"\n',
        encoding="utf-8",
    )

    run_init("codex", project, None)

    text = config.read_text(encoding="utf-8")
    assert '[mcp_servers.pel]' in text and "uvx" in text
    assert '[mcp_servers.other]' in text and '"keep-me"' in text  # 既有段保留
    assert '[model]' in text and '"deepseek-chat"' in text
    # 全局配置不写 --project（cwd 自动解析）
    assert "--project" not in text
    assert (project / "AGENTS.md").is_file()

    # 幂等：重跑不产生第二个 pel 段
    run_init("codex", project, None)
    assert config.read_text(encoding="utf-8").count("[mcp_servers.pel]") == 1


def test_deepcode_settings_merge(home: Path, tmp_path: Path, with_uv) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config = home / ".deepcode" / "settings.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"MODEL": "deepseek-v4", "mcpServers": {"existing": {"command": "x"}}}),
        encoding="utf-8",
    )

    run_init("deepcode", project, None)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["MODEL"] == "deepseek-v4"  # 用户字段保留
    assert data["mcpServers"]["existing"] == {"command": "x"}  # 既有服务器保留
    assert data["mcpServers"]["pel"]["command"] == "uvx"
    assert (project / "AGENTS.md").is_file()


def test_unknown_harness_and_missing_project(tmp_path: Path, with_uv) -> None:
    with pytest.raises(ScaffoldError):
        run_init("cursor", tmp_path, None)
    with pytest.raises(ScaffoldError):
        run_init("zcode", tmp_path / "nope", None)


def test_server_command_override(home: Path, tmp_path: Path, with_uv) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    run_init("zcode", project, "C:/tools/pel.exe mcp serve --project .")
    cfg = json.loads((project / ".zcode" / "config.json").read_text(encoding="utf-8"))
    entry = cfg["mcp"]["servers"]["pel"]
    assert entry["command"] == "C:/tools/pel.exe"
    assert entry["args"][-1] == "."  # 覆盖命令时 --project 不强插
