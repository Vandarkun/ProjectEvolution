"""配置加载测试：默认值、覆盖、损坏 YAML、API Key 不泄漏。"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_evolution.config import load_config
from project_evolution.errors import ConfigError


def test_defaults_when_no_config_file(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.schema_version == 1
    assert config.events_dir == "events"
    assert config.runtime_dir == ".runtime"
    assert config.analyzer_model is None
    assert config.analyzer_api_key_env is None
    assert config.analyzer_api_key is None


def test_yaml_override(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text(
        "events_dir: events-alt\nruntime_dir: runtime-alt\n"
        "analyzer_model: cheap-model\nanalyzer_api_key_env: PEL_TEST_KEY\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.events_dir == "events-alt"
    assert config.runtime_dir == "runtime-alt"
    assert config.analyzer_model == "cheap-model"
    assert config.analyzer_api_key_env == "PEL_TEST_KEY"


def test_partial_override_keeps_defaults(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text("events_dir: custom\n", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.events_dir == "custom"
    assert config.runtime_dir == ".runtime"  # 默认值保留


def test_corrupt_yaml_raises_config_error(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text(
        "events_dir: [unclosed\n  - broken", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_non_mapping_root_raises_config_error(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_unknown_key_raises_config_error(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text("totally_unknown: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_invalid_value_type_raises_config_error(tmp_path: Path) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text("schema_version: not-an-int\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_api_key_resolved_from_env_but_never_dumped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-super-secret-value"
    monkeypatch.setenv("PEL_TEST_KEY", secret)
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text(
        "analyzer_api_key_env: PEL_TEST_KEY\n", encoding="utf-8"
    )

    config = load_config(tmp_path)
    # 可通过环境变量解析
    assert config.analyzer_api_key == secret
    # 但配置文件与 dump 输出只包含环境变量名，永不包含明文
    assert "PEL_TEST_KEY" in (pel_dir / "config.yaml").read_text(encoding="utf-8")
    dumped = config.model_dump_json()
    assert "PEL_TEST_KEY" in dumped
    assert secret not in dumped
    assert secret not in str(config)


def test_api_key_none_when_env_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PEL_MISSING_KEY", raising=False)
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text(
        "analyzer_api_key_env: PEL_MISSING_KEY\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.analyzer_api_key is None
