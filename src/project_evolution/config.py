"""配置加载：默认配置 + ``.project-evolution/config.yaml`` 覆盖。

安全约定：API Key 只能通过环境变量引用（``analyzer_api_key_env`` 保存变量名），
配置文件与 ``model_dump`` 输出永不包含明文 key。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from project_evolution.errors import ConfigError

PEL_DIR_NAME = ".project-evolution"
CONFIG_FILENAME = "config.yaml"


class ChangeDigestConfig(BaseModel):
    """change_digest 预算（07-storage-and-index.md §8）。"""

    model_config = ConfigDict(extra="forbid")

    max_files: int = 50
    max_total_diff_chars: int = 40_000


class PrivacyConfig(BaseModel):
    """privacy.exclude：额外排除模式（只能追加；.project-evolution/ 硬编码不可关闭）。"""

    model_config = ConfigDict(extra="forbid")

    exclude: list[str] = Field(default_factory=lambda: [".env", "*.pem", "*.key"])


class EvidenceConfig(BaseModel):
    """Evidence patch 预算（07-storage-and-index.md §10）。"""

    model_config = ConfigDict(extra="forbid")

    commit_diffs: bool = True
    max_patch_bytes: int = 200_000


class PelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    #: PEL 数据目录下的子目录（相对 ``.project-evolution/``）
    events_dir: str = "events"
    runtime_dir: str = ".runtime"
    #: Evolution Model 的模型名与 API Key 环境变量名（仅名字，永不保存明文）
    analyzer_model: str | None = None
    analyzer_api_key_env: str | None = None
    change_digest: ChangeDigestConfig = Field(default_factory=ChangeDigestConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)

    @property
    def analyzer_api_key(self) -> str | None:
        """从环境变量解析 API Key；未配置或变量不存在时返回 None。"""
        if self.analyzer_api_key_env is None:
            return None
        return os.environ.get(self.analyzer_api_key_env)


def config_path(project_root: str | Path) -> Path:
    return Path(project_root) / PEL_DIR_NAME / CONFIG_FILENAME


def load_config(project_root: str | Path) -> PelConfig:
    """加载项目配置。

    - 项目根目录或配置文件不存在时返回纯默认配置；
    - YAML 损坏、根节点不是 mapping、字段非法、未知字段 -> :class:`ConfigError`。

    :raises ConfigError: 配置文件无法解析或不合法。
    """
    path = config_path(project_root)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}: {path}")
        data = raw
    try:
        return PelConfig.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(f"invalid config values in {path}: {exc}") from exc
