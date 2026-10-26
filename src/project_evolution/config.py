"""配置加载：默认配置 + ``.project-evolution/config.yaml`` 覆盖。

配置文件：项目根 ``pel.config.yaml``（2026-10 起以文件为主，环境变量只作后备）：

- 分析器的全部接线（provider / model / base_url / key）都写在 config.yaml，
  改配置不用动环境变量或启动命令；
- key 有两种写法：``analyzer_api_key``（明文，方便）或
  ``analyzer_api_key_env``（引用环境变量名，CI 友好）；两者都在时明文优先；
- 配置在项目根 ``pel.config.yaml``（与项目自身配置同级），真实配置不入库
  （gitignore 精确排除该文件名），仓库提交 ``pel.config.yaml.example`` 模板
  （``pel init`` 自动生成这两样）；``.project-evolution/`` 是纯生成事实目录，
  整目录忽略、不放任何手编文件。
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
#: 配置文件在项目根（与项目自身配置同级）；.project-evolution/ 是纯生成
#: 事实目录（events/evidence/runtime），不放任何手编文件。
CONFIG_FILENAME = "pel.config.yaml"
#: 兼容旧位置（.project-evolution/config.yaml）；新位置优先
_LEGACY_CONFIG = ("config.yaml",)


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
    #: Evolution Model 接线（provider: mock | openai | ollama）。
    #: key 支持明文 analyzer_api_key（优先）或 analyzer_api_key_env 引用环境变量。
    analyzer_provider: str | None = None
    analyzer_model: str | None = None
    analyzer_base_url: str | None = None
    analyzer_api_key: str | None = None
    analyzer_api_key_env: str | None = None
    change_digest: ChangeDigestConfig = Field(default_factory=ChangeDigestConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)

    @property
    def analyzer_api_key_resolved(self) -> str | None:
        """解析 API Key：明文配置优先，其次按环境变量名读取。"""
        if self.analyzer_api_key:
            return self.analyzer_api_key
        if self.analyzer_api_key_env is None:
            return None
        return os.environ.get(self.analyzer_api_key_env)


def config_path(project_root: str | Path) -> Path:
    return Path(project_root) / CONFIG_FILENAME


def load_config(project_root: str | Path) -> PelConfig:
    """加载项目配置。

    - 项目根目录或配置文件不存在时返回纯默认配置；
    - YAML 损坏、根节点不是 mapping、字段非法、未知字段 -> :class:`ConfigError`。

    :raises ConfigError: 配置文件无法解析或不合法。
    """
    path = config_path(project_root)
    if not path.is_file():
        legacy = Path(project_root) / PEL_DIR_NAME / "config.yaml"
        if legacy.is_file():
            path = legacy
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
