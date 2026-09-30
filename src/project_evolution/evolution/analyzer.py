"""Analyzer 请求/响应模型与 Provider 协议（05-evolution-analyzer.md §3/§7/§8）。

设计约束：

- 输入只包含 Task/Checkpoint 摘要与 ChangeDigest，不发送完整项目或完整历史；
- ``CandidateEvent.type`` 故意放宽为 ``str``：非法类型必须能被解析后交给
  Validator 出结构化诊断（phase-05 §6"非法 event type"），而不是在边界
  直接抛 pydantic 异常；
- Provider 只负责模型调用，不负责 Ledger、Git 与业务校验（05 §7）。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from project_evolution.evolution.change_digest import ChangeDigest

#: 分析管线版本（区别于 Prompt 版本与 Provider 模型版本，05-evolution-analyzer.md §15）
ANALYZER_VERSION = "pel-evolution-analyzer/0.1.0"

#: V0.1 允许的语义类型（03-event-model.md §6）
EVOLUTION_EVENT_TYPES = ("feature_change", "parameter_change", "decision", "bug_fix")

AnalyzerMode = Literal["checkpoint", "complete", "reprocess"]


class ProjectSummary(BaseModel):
    project_id: str
    name: str = ""


class TaskSummary(BaseModel):
    task_id: str
    title: str
    status: str | None = None


class CheckpointSummary(BaseModel):
    checkpoint_id: str
    index: int
    reason: str | None = None
    summary: str | None = None


class EvolutionEventSummary(BaseModel):
    """提供给模型的近期事件摘要（去重上下文，05 §11 Existing-event Lookup）。"""

    event_id: str
    type: str
    title: str
    detail: str = ""


class AnalyzerRequest(BaseModel):
    schema_version: int = 1
    project: ProjectSummary
    task: TaskSummary
    checkpoint: CheckpointSummary
    digest: ChangeDigest
    relevant_history: list[EvolutionEventSummary] = Field(default_factory=list)
    mode: AnalyzerMode = "checkpoint"


class CandidateEvent(BaseModel):
    """模型提出的候选事件；尚未通过 Validator，不是 Canonical Event。"""

    model_config = ConfigDict(extra="ignore")

    type: str
    title: str = ""
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AnalyzerResponse(BaseModel):
    """模型输出 Schema（05 §8）：``{"events": [...]}``，允许 ``events=[]``。"""

    model_config = ConfigDict(extra="ignore")

    events: list[CandidateEvent] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    #: 原始输出（诊断/评估用，永不写入 Ledger）
    raw_output: str | None = None


@runtime_checkable
class EvolutionModelProvider(Protocol):
    """Evolution Model Provider 协议（05 §7）。"""

    name: str

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerResponse: ...
