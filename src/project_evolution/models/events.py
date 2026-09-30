"""事件信封（Envelope）：RawEvent / EvolutionEvent。

字段与 docs/01-architecture/02-data-model.md §8/§10 严格对齐。

两条重要约定：

- ``seq`` 由 Ledger 在项目级写锁内分配（同 Ledger 内从 1 单调递增、不可复用），
  调用方构造事件时必须留空（None）；它是唯一的 Canonical 排序键。
- ``timestamp`` 仅作展示与粗略过滤元数据，多 Session/多 Agent 时钟偏移不影响排序。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, model_validator

#: V0.1 支持的 EvolutionEvent 类型（02-data-model.md §10）
EvolutionEventType = Literal["feature_change", "parameter_change", "decision", "bug_fix"]

#: 已知 kind 的 payload 必填字段（03-event-model.md §3 中明确标注"至少包含"的部分，
#: 加上 phase-03 生命周期采集层补充的 kind）。未知 kind 不做约束，保持前向兼容。
_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "task_started": ("title",),
    "task_completed": ("summary", "final_ref"),
    "checkpoint_created": ("index",),
    "session_started": ("agent",),
    "task_session_bound": ("agent",),
    "file_added": ("path",),
    "file_modified": ("path",),
    "file_deleted": ("path",),
    "test_started": ("command",),
    "test_passed": ("command",),
    "test_failed": ("command",),
    "git_commit": ("sha",),
    "git_tag": ("tag", "commit"),
}


class RawEvent(BaseModel):
    """确定性开发事实的统一信封。payload 结构由 kind 决定。"""

    schema_version: int = 1
    id: str
    seq: int | None = None
    project_id: str
    timestamp: datetime
    kind: str
    source: str
    task_id: str | None = None
    session_id: str | None = None
    checkpoint_id: str | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_payload_shape(self) -> RawEvent:
        required = _REQUIRED_PAYLOAD_FIELDS.get(self.kind)
        if required:
            missing = [key for key in required if key not in self.payload]
            if missing:
                raise ValueError(
                    f"RawEvent kind {self.kind!r} payload missing required field(s): {missing}"
                )
        return self


class EvolutionEvent(BaseModel):
    """经过语义分析形成的工程事件信封。必须引用 Evidence（人工事件除外）。

    phase-05 §5 追加的分析溯源字段（05-evolution-analyzer.md §15）：

    - ``analysis_batch_id``：同一次 Checkpoint 分析产生的事件共享一个 ANL- 批次
      （04-event-ledger.md §6，整批提交，不出现半提交）；
    - ``prompt_version``：产生该事件使用的 Prompt 版本（如 ``evolution-v1``）；
    - ``analysis_key``：幂等分析键（project+task+checkpoint+analyzer_version+digest_hash），
      同键已成功提交则拒绝重复追加（03-event-model.md §12）。
    """

    schema_version: int = 1
    id: str
    seq: int | None = None
    project_id: str
    task_id: str | None
    checkpoint_id: str | None
    timestamp: datetime
    type: EvolutionEventType
    title: str
    description: str
    evidence_ids: list[str]
    confidence: float | None = None
    analyzer_version: str
    prompt_version: str
    analysis_batch_id: str
    analysis_key: str
    payload: dict[str, Any]
