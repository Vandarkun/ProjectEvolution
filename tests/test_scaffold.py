"""pel init 脚手架：幂等合并、不覆盖既有配置、AGENTS.md 只追加一次。

用户级配置（codex/dsh）通过 monkeypatch Path.home 隔离到 tmp_path。
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
    # 零参数启动：provider/base_url/key 全在项目 config.yaml
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
    assert not again.written and len(again.skipped) == 3

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


def test_dsh_cordis_yml(home: Path, tmp_path: Path, with_uv, monkeypatch) -> None:
    """dsh（DeepSeek Harness）：cordis.yml 插件条目三种形态 + 幂等。"""
    dsh_home = home / ".dsh"
    monkeypatch.delenv("DSH_HOME", raising=False)
    project = tmp_path / "proj"
    project.mkdir()

    # 1) 全新安装
    run_init("dsh", project, None)
    cfg = dsh_home / "cordis.yml"
    text = cfg.read_text(encoding="utf-8")
    assert "plugins:" in text and "- id: pel" in text
    assert "'@deepseek-ai/dsh-mcp-client'" in text and "command:" in text
    assert (project / "AGENTS.md").is_file()

    # 2) 幂等：重跑不重复
    run_init("dsh", project, None)
    assert cfg.read_text(encoding="utf-8").count("- id: pel") == 1

    # 3) 已有其他插件：保留，pel 挂在同一 plugins: 键下
    cfg.write_text(
        "plugins:\n"
        "  - id: other\n"
        "    name: '@deepseek-ai/dsh-mcp-client'\n"
        "    config:\n"
        "      serverName: other\n",
        encoding="utf-8",
    )
    run_init("dsh", project, None)
    text = cfg.read_text(encoding="utf-8")
    assert "- id: other" in text and "- id: pel" in text

    # 4) 整文件即插件列表（裸列表形态）
    cfg.write_text(
        "- id: first\n  name: x\n", encoding="utf-8"
    )
    run_init("dsh", project, None)
    text = cfg.read_text(encoding="utf-8")
    assert "- id: first" in text and "- id: pel" in text


def test_dsh_home_env(home: Path, tmp_path: Path, with_uv, monkeypatch) -> None:
    """DSH_HOME 环境变量优先于 ~/.dsh。"""
    custom = tmp_path / "custom-dsh"
    monkeypatch.setenv("DSH_HOME", str(custom))
    project = tmp_path / "proj"
    project.mkdir()
    run_init("deepseek-harness", project, None)  # 别名同 dsh
    assert (custom / "cordis.yml").is_file()


def test_analyzer_config_fill_only_missing(home: Path, tmp_path: Path, with_uv) -> None:
    """项目根 pel.config.yaml 只填缺失键：用户已配置的不被覆盖。"""
    import yaml

    project = tmp_path / "proj"
    project.mkdir()
    run_init("zcode", project, None)
    cfg = yaml.safe_load((project / "pel.config.yaml").read_text("utf-8"))
    assert cfg["analyzer_model"] == "deepseek-chat"
    assert cfg["analyzer_api_key_env"] == "DEEPSEEK_API_KEY"

    # 用户改用自己的接线后重跑：既有选择不被覆盖（缺失键允许补齐）
    (project / "pel.config.yaml").write_text(
        "analyzer_provider: ollama\nanalyzer_model: my-model\n"
        "analyzer_base_url: http://localhost:11434\n"
        "analyzer_api_key_env: MY_KEY\n",
        encoding="utf-8",
    )
    result = run_init("zcode", project, None)
    assert "pel.config.yaml (configured)" in result.skipped
    cfg = yaml.safe_load((project / "pel.config.yaml").read_text("utf-8"))
    assert cfg["analyzer_model"] == "my-model"


def test_example_template_and_gitignore(home: Path, tmp_path: Path, with_uv) -> None:
    """配置在项目根；.project-evolution/ 整目录忽略（纯事实目录）；example 可入库。"""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".gitignore").write_text(".project-evolution/\n", encoding="utf-8")

    run_init("zcode", project, None)

    gi_lines = [
        ln.strip() for ln in (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    # 事实目录整目录忽略；真实配置按文件名精确排除（example 天然可入库）
    assert ".project-evolution/" in gi_lines
    assert "pel.config.yaml" in gi_lines
    assert "!.project-evolution/config.yaml.example" not in gi_lines

    example = project / "pel.config.yaml.example"
    assert example.is_file() and "sk-xxxxxxxx" in example.read_text(encoding="utf-8")
    import yaml

    cfg = yaml.safe_load(
        (project / "pel.config.yaml").read_text(encoding="utf-8")
    )
    assert cfg["analyzer_provider"] == "openai"
    assert cfg["analyzer_base_url"] == "https://api.deepseek.com"
