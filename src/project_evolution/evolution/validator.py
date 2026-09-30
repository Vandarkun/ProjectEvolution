"""Evolution Validator（phase-05 §6，05-evolution-analyzer.md §10）。

模型只能提出结构化候选事件；能否进入 Canonical Ledger 由本模块决定。
V0.1 策略：证据不足一律 ``reject`` 并记录诊断（不引入 candidate 状态）。

至少拒绝（phase-05 §6 清单，括号内为诊断 code）：

1. 非法 event type（INVALID_EVENT_TYPE）；
2. 缺少 Evidence（MISSING_EVIDENCE）；
3. 不存在 Evidence ID（EVIDENCE_UNKNOWN）；
4. Evidence ID 不在本 Checkpoint Digest 清单内（EVIDENCE_NOT_IN_DIGEST）；
5. Parameter before=after 且没有合理解释（PARAM_BEFORE_EQUALS_AFTER）；
6. 空 title（EMPTY_TITLE）；
7. 超长字段（FIELD_TOO_LONG）；
8. 重复 analysis key（DUPLICATE_ANALYSIS_KEY，批次级）。

另按 05 §10 追加：payload 不符合类型 Schema（INVALID_PAYLOAD）、
parameter 缺 before/after（PARAM_MISSING_VALUES）、
bug_fix 证据无 diff/测试（BUG_FIX_EVIDENCE_WEAK）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from project_evolution.evolution.analyzer import (
    EVOLUTION_EVENT_TYPES,
    CandidateEvent,
)

MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 4_000
MAX_PAYLOAD_CHARS = 8_000

#: payload Schema 按类型绑定（03-event-model.md §6）；延迟导入避免循环依赖
_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {}


def _payload_model(event_type: str) -> type[BaseModel] | None:
    if not _PAYLOAD_MODELS:
        from project_evolution.models.evolution import (  # noqa: PLC0415
            BugFix,
            Decision,
            FeatureChange,
            ParameterChange,
        )

        _PAYLOAD_MODELS.update(
            {
                "feature_change": FeatureChange,
                "parameter_change": ParameterChange,
                "decision": Decision,
                "bug_fix": BugFix,
            }
        )
    return _PAYLOAD_MODELS.get(event_type)


class Rejection(BaseModel):
    """一条候选事件被拒绝的诊断（精确到事件与原因）。"""

    code: str
    message: str
    event_index: int
    event_title: str = ""
    event_type: str = ""


class ValidationOutcome(BaseModel):
    accepted: list[CandidateEvent] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)

    @property
    def accepted_types(self) -> list[str]:
        return [event.type for event in self.accepted]


class ValidatorContext(BaseModel):
    """Validator 依赖的外部事实（由 AnalysisService 从 Ledger 收集）。"""

    #: 本 Checkpoint Digest 的 evidence 清单（模型唯一合法来源）
    digest_evidence: dict[str, str] = Field(default_factory=dict)  # id -> type
    #: 项目内全部已知 Evidence ID（区分"不存在"与"不在清单"）
    known_evidence_ids: set[str] = Field(default_factory=set)
    #: 本批次 analysis_key
    analysis_key: str = ""
    #: 账本中已成功提交的 analysis_key（幂等）
    existing_analysis_keys: set[str] = Field(default_factory=set)

    def digest_contains(self, evidence_id: str) -> bool:
        return evidence_id in self.digest_evidence


def validate_candidates(
    candidates: list[CandidateEvent], context: ValidatorContext
) -> ValidationOutcome:
    """校验一批候选事件：逐条独立判定，坏事件不拖累好事件。

    批次级失败（重复 analysis key）拒绝整批——同一批次的其余事件
    应与失败的分析一起重试，而不是部分提交。
    """
    if context.analysis_key and context.analysis_key in context.existing_analysis_keys:
        return ValidationOutcome(
            rejections=[
                Rejection(
                    code="DUPLICATE_ANALYSIS_KEY",
                    message=(
                        "analysis_key already committed successfully; "
                        "retry must reuse the existing batch instead of appending"
                    ),
                    event_index=-1,
                )
            ]
        )

    outcome = ValidationOutcome()
    for index, event in enumerate(candidates):
        rejection = _check_event(event, index, context)
        if rejection is None:
            outcome.accepted.append(event)
        else:
            outcome.rejections.append(rejection)
    return outcome


# --------------------------------------------------------------------- inside


def _check_event(
    event: CandidateEvent, index: int, context: ValidatorContext
) -> Rejection | None:
    def reject(code: str, message: str) -> Rejection:
        return Rejection(
            code=code, message=message, event_index=index, event_title=event.title,
            event_type=event.type,
        )

    # 1. 非法 event type
    if event.type not in EVOLUTION_EVENT_TYPES:
        return reject(
            "INVALID_EVENT_TYPE",
            f"type {event.type!r} not in {list(EVOLUTION_EVENT_TYPES)}",
        )

    # 6. 空 title
    if not event.title.strip():
        return reject("EMPTY_TITLE", "title must be a non-empty string")

    # 7. 超长字段
    if len(event.title) > MAX_TITLE_CHARS:
        return reject(
            "FIELD_TOO_LONG", f"title exceeds {MAX_TITLE_CHARS} chars ({len(event.title)})"
        )
    if len(event.description) > MAX_DESCRIPTION_CHARS:
        return reject(
            "FIELD_TOO_LONG",
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars ({len(event.description)})",
        )
    payload_json = json.dumps(event.payload, ensure_ascii=False)
    if len(payload_json) > MAX_PAYLOAD_CHARS:
        return reject(
            "FIELD_TOO_LONG",
            f"payload exceeds {MAX_PAYLOAD_CHARS} chars ({len(payload_json)})",
        )

    # payload Schema（按类型）
    model = _payload_model(event.type)
    assert model is not None  # EVOLUTION_EVENT_TYPES 与模型表一一对应
    try:
        parsed: BaseModel = model.model_validate(event.payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        return reject("INVALID_PAYLOAD", f"payload .{loc}: {first['msg']}")

    # 2. 缺少 Evidence
    if not event.evidence_ids:
        return reject("MISSING_EVIDENCE", "every event must cite at least one evidence id")

    # 3/4. Evidence 存在性 + Digest 清单子集校验
    for evidence_id in event.evidence_ids:
        if evidence_id not in context.known_evidence_ids:
            return reject(
                "EVIDENCE_UNKNOWN", f"evidence id {evidence_id!r} does not exist in this project"
            )
        if not context.digest_contains(evidence_id):
            return reject(
                "EVIDENCE_NOT_IN_DIGEST",
                f"evidence id {evidence_id!r} exists but is not in this checkpoint digest "
                "(hallucinated or borrowed from another checkpoint)",
            )

    # 类型特定规则
    if event.type == "parameter_change":
        payload_dict: dict[str, Any] = parsed.model_dump()
        before, after = payload_dict.get("before"), payload_dict.get("after")
        reason = payload_dict.get("reason")
        if before is None and after is None:
            return reject(
                "PARAM_MISSING_VALUES",
                "parameter_change must carry before and/or after findable in evidence",
            )
        if before is not None and before == after and not (reason or "").strip():
            return reject(
                "PARAM_BEFORE_EQUALS_AFTER",
                "before == after without an evidenced reason; emit nothing instead",
            )
    if event.type == "bug_fix":
        cited_types = {context.digest_evidence.get(eid) for eid in event.evidence_ids}
        if not cited_types & {"git_diff", "test_result"}:
            return reject(
                "BUG_FIX_EVIDENCE_WEAK",
                "bug_fix needs at least one git_diff or test_result evidence",
            )
    return None
