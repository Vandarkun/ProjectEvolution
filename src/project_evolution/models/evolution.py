"""EvolutionEvent 的 payload 模型与派生 ProjectState。

字段与 docs/01-architecture/02-data-model.md §11-§14、§17 严格对齐。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FeatureChange(BaseModel):
    """用户/工程可理解的能力变化，而不是单纯文件名。"""

    feature: str
    action: Literal["added", "modified", "removed"]
    scope: str | None = None
    before: str | None = None
    after: str | None = None


class ParameterChange(BaseModel):
    """有工程意义的参数/配置变化；参数不限于数值。"""

    name: str
    before: str | None
    after: str | None
    unit: str | None = None
    scope: str | None = None
    reason: str | None = None


class Decision(BaseModel):
    """经过选择形成的工程约定；被替代时保留旧事件并通过 supersedes 建立关系。"""

    subject: str
    decision: str
    reason: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)


class BugFix(BaseModel):
    """具有后续工程价值的 Bug 与修复。V0.1 将 Bug 与 Fix 合成单个语义事件。"""

    problem: str
    cause: str | None = None
    fix: str
    affected_scope: list[str] = Field(default_factory=list)
    verification: str | None = None


class ProjectState(BaseModel):
    """派生的项目当前状态；不是 Canonical Event，从 Ledger Projection 得到。"""

    active_features: list[str] = Field(default_factory=list)
    current_parameters: dict[str, str] = Field(default_factory=dict)
    active_decisions: list[str] = Field(default_factory=list)
    recent_bug_fixes: list[str] = Field(default_factory=list)
    latest_version: str | None = None
