"""EvolutionEventLedger 测试（phase-05 §5）：与 Raw Ledger 同级的稳定性保证。

覆盖：seq 分配、batch 原子提交、analysis_key 幂等拒绝、重放顺序、
损坏检测、尾部修复、统一 Replay API。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_evolution.errors import ValidationError
from project_evolution.ids import new_id
from project_evolution.ledger import (
    DuplicateAnalysisKeyError,
    DuplicateEventError,
    EvolutionEventLedger,
    LedgerCorruptionError,
    iter_evolution_events,
)
from project_evolution.ledger.jsonl import encode_record
from project_evolution.models import EvolutionEvent

NOW = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))


def evo_event(**overrides) -> EvolutionEvent:
    fields: dict = dict(
        id=new_id("EVO"),
        project_id="PRJ-test",
        task_id="TASK-1",
        checkpoint_id="CHK-1",
        timestamp=NOW,
        type="parameter_change",
        title="OpacityMin 0.2 -> 0.3",
        description="提高最低可见度",
        evidence_ids=[new_id("EVD")],
        analyzer_version="analyzer-0.1.0",
        prompt_version="evolution-v1",
        analysis_batch_id=new_id("ANL"),
        analysis_key=f"PRJ-test|TASK-1|CHK-{new_id('ANL')}|analyzer-0.1.0|digest-1",
        payload={"name": "OpacityMin", "before": "0.2", "after": "0.3"},
    )
    fields.update(overrides)
    return EvolutionEvent(**fields)


def line_for(event: EvolutionEvent, seq: int) -> bytes:
    stamped = event.model_copy(update={"seq": seq})
    return (encode_record(stamped.model_dump(mode="json")) + "\n").encode("utf-8")


# ----------------------------------------------------------------- 基础写入


def test_append_assigns_seq_and_returns_copy(tmp_path: Path) -> None:
    ledger = EvolutionEventLedger(tmp_path)
    event = evo_event()
    stamped = ledger.append(event)
    assert stamped.seq == 1
    assert event.seq is None  # 入参不被修改
    assert stamped.analysis_batch_id == event.analysis_batch_id
    second = ledger.append(evo_event())
    assert second.seq == 2


def test_append_rejects_preset_seq(tmp_path: Path) -> None:
    ledger = EvolutionEventLedger(tmp_path)
    with pytest.raises(ValidationError, match="seq is assigned by the ledger"):
        ledger.append(evo_event(seq=7))


def test_append_rejects_non_evolution_event(tmp_path: Path) -> None:
    ledger = EvolutionEventLedger(tmp_path)
    with pytest.raises(ValidationError, match="expected EvolutionEvent"):
        ledger.append("not an event")  # type: ignore[arg-type]


def test_duplicate_id_rejected_after_reload(tmp_path: Path) -> None:
    EvolutionEventLedger(tmp_path).append(evo_event(id="EVO-known"))
    fresh = EvolutionEventLedger(tmp_path)
    with pytest.raises(DuplicateEventError):
        fresh.append(evo_event(id="EVO-known"))


def test_batch_commits_atomically_with_shared_batch_id(tmp_path: Path) -> None:
    """同批事件共享 analysis_batch_id，一次性写入（04 §6：不出现半提交）。"""
    ledger = EvolutionEventLedger(tmp_path)
    batch_id = new_id("ANL")
    key = "PRJ-test|TASK-1|CHK-1|analyzer-0.1.0|digest-1"
    stamped = ledger.append_many(
        [
            evo_event(analysis_batch_id=batch_id, analysis_key=key),
            evo_event(
                analysis_batch_id=batch_id,
                analysis_key=key,
                type="decision",
                title="快捷键改用 Alt+Wheel",
                payload={"subject": "快捷键", "decision": "Alt+Wheel"},
            ),
        ]
    )
    assert [event.seq for event in stamped] == [1, 2]
    assert {event.analysis_batch_id for event in stamped} == {batch_id}
    events = list(ledger.iter_events())
    assert len(events) == 2
    assert [event.seq for event in events] == [1, 2]


# ------------------------------------------------------------------- 幂等


def test_duplicate_analysis_key_other_batch_rejected(tmp_path: Path) -> None:
    """同 analysis_key 的新批次被拒绝（03-event-model §12 / phase-05 §6）。"""
    key = "PRJ-test|TASK-1|CHK-1|analyzer-0.1.0|digest-1"
    EvolutionEventLedger(tmp_path).append(evo_event(analysis_key=key))
    fresh = EvolutionEventLedger(tmp_path)
    with pytest.raises(DuplicateAnalysisKeyError):
        fresh.append(evo_event(analysis_key=key, analysis_batch_id=new_id("ANL")))


def test_same_analysis_key_within_batch_allowed(tmp_path: Path) -> None:
    """同一批次的多条事件共享 analysis_key 是设计行为，不受幂等拒绝影响。"""
    key = "PRJ-test|TASK-1|CHK-1|analyzer-0.1.0|digest-1"
    batch_id = new_id("ANL")
    ledger = EvolutionEventLedger(tmp_path)
    stamped = ledger.append_many(
        [evo_event(analysis_key=key, analysis_batch_id=batch_id) for _ in range(3)]
    )
    assert len(stamped) == 3
    assert ledger.find_batch(key) == stamped


def test_find_batch_and_analysis_keys(tmp_path: Path) -> None:
    key = "PRJ-test|TASK-1|CHK-1|analyzer-0.1.0|digest-1"
    ledger = EvolutionEventLedger(tmp_path)
    assert ledger.find_batch(key) == []
    assert ledger.analysis_keys() == set()
    ledger.append(evo_event(analysis_key=key))
    assert ledger.analysis_keys() == {key}
    assert len(ledger.find_batch(key)) == 1


# ------------------------------------------------------------------- 损坏


def test_middle_corruption_raises(tmp_path: Path) -> None:
    path = tmp_path / ".project-evolution" / "events" / "evolution.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(line_for(evo_event(), 1) + b"{broken json\n" + line_for(evo_event(), 3))
    ledger = EvolutionEventLedger(tmp_path)
    with pytest.raises(LedgerCorruptionError):
        list(ledger.iter_events())


def test_unconfirmed_tail_skipped_and_repairable(tmp_path: Path) -> None:
    path = tmp_path / ".project-evolution" / "events" / "evolution.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(line_for(evo_event(), 1) + line_for(evo_event(), 2)[:20])
    ledger = EvolutionEventLedger(tmp_path)
    assert len(list(ledger.iter_events())) == 1
    report = ledger.repair_tail()
    assert report.status == "repaired"
    assert ledger.check_integrity().ok
    # 修复后可继续追加，seq 继续单调
    stamped = ledger.append(evo_event())
    assert stamped.seq == 2


def test_missing_analysis_field_record_rejected(tmp_path: Path) -> None:
    """缺 analysis_batch_id/prompt_version/analysis_key 的记录视为损坏。"""
    from project_evolution.ledger.integrity import LineValidationError, validate_evolution_record

    event = evo_event()
    record = event.model_dump(mode="json")
    record["seq"] = 1
    with pytest.raises(LineValidationError, match="schema_version"):
        validate_evolution_record({"id": "EVO-1"})
    for field in ("prompt_version", "analysis_batch_id", "analysis_key", "analyzer_version"):
        broken = dict(record)
        broken[field] = ""
        with pytest.raises(LineValidationError, match=field):
            validate_evolution_record(broken)


def test_bad_id_prefix_rejected() -> None:
    from project_evolution.ledger.integrity import LineValidationError, validate_evolution_record

    event = evo_event(id="RAW-notan-evo")
    record = event.model_dump(mode="json")
    record["seq"] = 1
    with pytest.raises(LineValidationError, match="must start with 'EVO-'"):
        validate_evolution_record(record)


# ------------------------------------------------------------------- replay


def test_iter_evolution_events_filters(tmp_path: Path) -> None:
    ledger = EvolutionEventLedger(tmp_path)
    ledger.append(evo_event(project_id="PRJ-a", task_id="TASK-1"))
    ledger.append(evo_event(project_id="PRJ-b", task_id="TASK-2"))
    ledger.append(evo_event(project_id="PRJ-a", task_id="TASK-3"))
    ids_a = [e.id for e in iter_evolution_events(tmp_path, project_id="PRJ-a")]
    assert len(ids_a) == 2
    ids_task2 = list(iter_evolution_events(tmp_path, task_id="TASK-2"))
    assert len(ids_task2) == 1


def test_reload_keeps_seq_monotonic_across_instances(tmp_path: Path) -> None:
    first = EvolutionEventLedger(tmp_path)
    first.append(evo_event())
    second = EvolutionEventLedger(tmp_path)
    stamped = second.append(evo_event())
    assert stamped.seq == 2


def test_events_path_defaults(tmp_path: Path) -> None:
    ledger = EvolutionEventLedger(tmp_path)
    assert ledger.events_path == (
        tmp_path / ".project-evolution" / "events" / "evolution.jsonl"
    )
