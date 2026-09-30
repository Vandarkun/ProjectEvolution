"""确定性去重（05-evolution-analyzer.md §11，phase-05 §3 Dedup/idempotency）。

V0.1 不调用昂贵模型做语义比较，只做确定性判重：

- normalized key（scope+参数名 / feature+action / subject+decision / problem）；
- exact before/after（parameter 与 feature 的取值完全一致才判重）；
- title token overlap（同类事件标题 token Jaccard >= 阈值）。

Checkpoint 级幂等由 analysis_key 承担（03-event-model.md §12）；
本模块负责"同一 Task 内不同 Checkpoint 重复描述同一事实"的去重。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from project_evolution.evolution.analyzer import CandidateEvent
from project_evolution.models import EvolutionEvent

#: 同类事件标题 token Jaccard 判重阈值
TITLE_OVERLAP_THRESHOLD = 0.8

_TOKEN_RE = re.compile(r"[\w]+")


def _normalize(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: m.group(0).lower(), text or "").strip()


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "") if len(token) > 1}


def _payload(event: CandidateEvent | EvolutionEvent) -> dict:
    return event.payload if isinstance(event.payload, dict) else {}


def normalized_key(event: CandidateEvent | EvolutionEvent) -> str:
    """事件的确定性身份键（05 §11 Deterministic Identity）。"""
    payload = _payload(event)
    if event.type == "parameter_change":
        scope = _normalize(str(payload.get("scope") or ""))
        name = _normalize(str(payload.get("name") or ""))
        return f"parameter|{scope}|{name}"
    if event.type == "feature_change":
        feature = _normalize(str(payload.get("feature") or ""))
        action = _normalize(str(payload.get("action") or ""))
        return f"feature|{feature}|{action}"
    if event.type == "decision":
        subject = _normalize(str(payload.get("subject") or ""))
        decision = _normalize(str(payload.get("decision") or ""))
        return f"decision|{subject}|{decision}"
    if event.type == "bug_fix":
        problem = _normalize(str(payload.get("problem") or ""))
        return f"bugfix|{problem}"
    return f"other|{_normalize(getattr(event, 'title', ''))}"


def _value_identity(event: CandidateEvent | EvolutionEvent) -> tuple:
    """取值身份：parameter/feature 的 before/after 完全一致才判重。"""
    payload = _payload(event)
    if event.type == "parameter_change":
        return (payload.get("before"), payload.get("after"))
    if event.type == "feature_change":
        return (payload.get("before"), payload.get("after"))
    return ()


def title_overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class DedupResult:
    kept: list[CandidateEvent]
    duplicates: list[tuple[int, str]]  # (candidate index, reason)


def dedup_candidates(
    candidates: list[CandidateEvent], existing: list[EvolutionEvent]
) -> DedupResult:
    """对候选事件去重：先批内判重，再对照同 Task 已有事件。

    判重条件（同类事件内）：

    - normalized key 相同，且取值身份（before/after）一致；或
    - 标题 token Jaccard >= :data:`TITLE_OVERLAP_THRESHOLD`。
    """
    kept: list[CandidateEvent] = []
    duplicates: list[tuple[int, str]] = []
    seen_keys: dict[str, CandidateEvent] = {}
    existing_keys = {normalized_key(event): event for event in existing}

    for index, candidate in enumerate(candidates):
        key = normalized_key(candidate)
        value = _value_identity(candidate)
        in_batch = seen_keys.get(key)
        if in_batch is not None and _value_identity(in_batch) == value:
            duplicates.append((index, f"duplicate of in-batch event: {key}"))
            continue
        rival = existing_keys.get(key)
        if rival is not None and _value_identity(rival) == value:
            duplicates.append((index, f"duplicates existing event {rival.id}: {key}"))
            continue
        title_dup = _find_title_duplicate(candidate, existing)
        if title_dup is not None:
            duplicates.append((index, f"title overlaps existing event {title_dup.id}"))
            continue
        seen_keys[key] = candidate
        kept.append(candidate)
    return DedupResult(kept=kept, duplicates=duplicates)


def _find_title_duplicate(
    candidate: CandidateEvent, existing: list[EvolutionEvent]
) -> EvolutionEvent | None:
    for event in existing:
        if event.type != candidate.type:
            continue
        if title_overlap(candidate.title, event.title) >= TITLE_OVERLAP_THRESHOLD:
            return event
    return None
