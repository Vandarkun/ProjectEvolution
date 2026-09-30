"""Validator 测试（phase-05 §6）：逐条拒绝规则 + 通过路径。"""

from __future__ import annotations

from project_evolution.evolution.analyzer import CandidateEvent
from project_evolution.evolution.validator import (
    ValidatorContext,
    validate_candidates,
)


def context(**overrides) -> ValidatorContext:
    fields: dict = dict(
        digest_evidence={
            "EVD-1": "git_diff",
            "EVD-2": "test_result",
            "EVD-3": "raw_event",
        },
        known_evidence_ids={"EVD-1", "EVD-2", "EVD-3", "EVD-99"},
        analysis_key="PRJ|TASK|CHK|analyzer|digest",
        existing_analysis_keys=set(),
    )
    fields.update(overrides)
    return ValidatorContext(**fields)


def candidate(**overrides) -> CandidateEvent:
    fields: dict = dict(
        type="parameter_change",
        title="OpacityMin 0.2 -> 0.3",
        description="提高最低可见度",
        evidence_ids=["EVD-1"],
        payload={"name": "OpacityMin", "before": "0.2", "after": "0.3"},
    )
    fields.update(overrides)
    return CandidateEvent(**fields)


# ------------------------------------------------------------------- 通过


def test_valid_event_accepted() -> None:
    outcome = validate_candidates([candidate()], context())
    assert outcome.rejections == []
    assert len(outcome.accepted) == 1


def test_good_event_survives_bad_event_in_same_batch() -> None:
    outcome = validate_candidates(
        [candidate(), candidate(type="nonsense_type")], context()
    )
    assert len(outcome.accepted) == 1
    assert outcome.rejections[0].code == "INVALID_EVENT_TYPE"
    assert outcome.rejections[0].event_index == 1


def test_empty_events_is_valid_noop() -> None:
    outcome = validate_candidates([], context())
    assert outcome.accepted == [] and outcome.rejections == []


# ------------------------------------------------------------ §6 拒绝清单


def test_invalid_event_type_rejected() -> None:
    outcome = validate_candidates([candidate(type="refactor")], context())
    assert outcome.rejections[0].code == "INVALID_EVENT_TYPE"


def test_missing_evidence_rejected() -> None:
    outcome = validate_candidates([candidate(evidence_ids=[])], context())
    assert outcome.rejections[0].code == "MISSING_EVIDENCE"


def test_unknown_evidence_id_rejected() -> None:
    outcome = validate_candidates([candidate(evidence_ids=["EVD-404"])], context())
    assert outcome.rejections[0].code == "EVIDENCE_UNKNOWN"


def test_evidence_outside_digest_rejected() -> None:
    """EVD-99 项目里存在，但不在本 checkpoint digest 清单 -> 拒绝。"""
    outcome = validate_candidates([candidate(evidence_ids=["EVD-99"])], context())
    assert outcome.rejections[0].code == "EVIDENCE_NOT_IN_DIGEST"


def test_parameter_before_equals_after_without_reason_rejected() -> None:
    outcome = validate_candidates(
        [candidate(payload={"name": "X", "before": "1", "after": "1"})], context()
    )
    assert outcome.rejections[0].code == "PARAM_BEFORE_EQUALS_AFTER"


def test_parameter_before_equals_after_with_reason_accepted() -> None:
    """before == after 但有证据化解释（例如单位/语义修正）时不拒。"""
    outcome = validate_candidates(
        [
            candidate(
                payload={"name": "X", "before": "1", "after": "1", "reason": "语义澄清：值不变，单位从秒改为毫秒读法"}
            )
        ],
        context(),
    )
    assert outcome.rejections == []


def test_empty_title_rejected() -> None:
    outcome = validate_candidates([candidate(title="   ")], context())
    assert outcome.rejections[0].code == "EMPTY_TITLE"


def test_overlong_title_rejected() -> None:
    outcome = validate_candidates([candidate(title="x" * 201)], context())
    assert outcome.rejections[0].code == "FIELD_TOO_LONG"


def test_overlong_description_rejected() -> None:
    outcome = validate_candidates([candidate(description="y" * 4001)], context())
    assert outcome.rejections[0].code == "FIELD_TOO_LONG"


def test_overlong_payload_rejected() -> None:
    outcome = validate_candidates(
        [candidate(payload={"name": "X", "before": "z" * 9000, "after": "1"})], context()
    )
    assert outcome.rejections[0].code == "FIELD_TOO_LONG"


def test_duplicate_analysis_key_rejects_whole_batch() -> None:
    ctx = context(existing_analysis_keys={"PRJ|TASK|CHK|analyzer|digest"})
    outcome = validate_candidates([candidate()], ctx)
    assert outcome.accepted == []
    assert outcome.rejections[0].code == "DUPLICATE_ANALYSIS_KEY"


# ------------------------------------------------------- 附加类型级规则


def test_invalid_payload_schema_rejected() -> None:
    """decision 必须有 subject/decision；缺失字段按 Schema 拒绝。"""
    outcome = validate_candidates(
        [candidate(type="decision", payload={"subject": "only subject"})], context()
    )
    assert outcome.rejections[0].code == "INVALID_PAYLOAD"


def test_parameter_missing_both_values_rejected() -> None:
    """before/after 显式为 null 且无证据可依 -> 拒绝（缺失字段走 INVALID_PAYLOAD）。"""
    outcome = validate_candidates(
        [candidate(payload={"name": "OpacityMin", "before": None, "after": None})],
        context(),
    )
    assert outcome.rejections[0].code == "PARAM_MISSING_VALUES"
    missing = validate_candidates([candidate(payload={"name": "OpacityMin"})], context())
    assert missing.rejections[0].code == "INVALID_PAYLOAD"


def test_bug_fix_requires_diff_or_test_evidence() -> None:
    weak = candidate(
        type="bug_fix",
        title="fix crash",
        payload={"problem": "crash", "fix": "move to UI thread"},
        evidence_ids=["EVD-3"],  # 仅 raw_event 证据
    )
    strong = weak.model_copy(update={"evidence_ids": ["EVD-1"]})
    outcome = validate_candidates([weak, strong], context())
    codes = [r.code for r in outcome.rejections]
    assert codes == ["BUG_FIX_EVIDENCE_WEAK"]
    assert len(outcome.accepted) == 1
