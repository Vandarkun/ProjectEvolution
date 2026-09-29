"""RawEventLedger 测试：seq 分配、批量原子性、重放顺序、损坏检测与修复。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_evolution.errors import ValidationError
from project_evolution.ids import new_id
from project_evolution.ledger import (
    DuplicateEventError,
    LedgerCorruptionError,
    RawEventLedger,
    iter_raw_events,
)
from project_evolution.ledger.jsonl import encode_record
from project_evolution.models import RawEvent

NOW = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))


def raw_event(**overrides) -> RawEvent:
    fields: dict = dict(
        id=new_id("RAW"),
        project_id="PRJ-test",
        timestamp=NOW,
        kind="file_modified",
        source="cli",
        payload={"path": "Core/Service.cs"},
    )
    fields.update(overrides)
    return RawEvent(**fields)


def line_for(event: RawEvent, seq: int) -> bytes:
    stamped = event.model_copy(update={"seq": seq})
    return (encode_record(stamped.model_dump(mode="json")) + "\n").encode("utf-8")


# ----------------------------------------------------------------- 基础写入


def test_append_assigns_seq_and_returns_copy(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    event = raw_event()
    stamped = ledger.append(event)
    assert stamped.seq == 1
    assert event.seq is None  # 入参不被修改
    second = ledger.append(raw_event())
    assert second.seq == 2


def test_append_rejects_preset_seq(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    event = raw_event(seq=7)
    with pytest.raises(ValidationError, match="seq is assigned by the ledger"):
        ledger.append(event)


def test_append_rejects_non_raw_event(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    with pytest.raises(ValidationError, match="expected RawEvent"):
        ledger.append("not an event")  # type: ignore[arg-type]


def test_duplicate_id_rejected_same_instance(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    event = raw_event()
    ledger.append(event)
    with pytest.raises(DuplicateEventError):
        ledger.append(raw_event(id=event.id))


def test_duplicate_id_rejected_after_reload(tmp_path: Path) -> None:
    RawEventLedger(tmp_path).append(raw_event(id="RAW-known"))
    fresh = RawEventLedger(tmp_path)
    with pytest.raises(DuplicateEventError):
        fresh.append(raw_event(id="RAW-known"))


def test_duplicate_id_rejected_within_batch(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    batch = [raw_event(id="RAW-dup"), raw_event(id="RAW-dup")]
    with pytest.raises(DuplicateEventError, match="within batch"):
        ledger.append_many(batch)


def test_append_many_assigns_consecutive_seqs(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    batch = [raw_event() for _ in range(5)]
    stamped = ledger.append_many(batch)
    assert [e.seq for e in stamped] == [1, 2, 3, 4, 5]
    again = ledger.append_many([raw_event()])
    assert again[0].seq == 6


def test_events_file_layout(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(raw_event())
    expected_path = tmp_path / ".project-evolution" / "events" / "raw.jsonl"
    assert ledger.events_path == expected_path
    data = expected_path.read_bytes()
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert (tmp_path / ".project-evolution" / ".runtime" / "locks" / "ledger.lock").exists()


def test_multi_instance_seq_continuity(tmp_path: Path) -> None:
    seqs = []
    for _ in range(5):
        # 每次都用全新实例：验证增量索引能跨实例同步其他 writer 的追加
        ledger = RawEventLedger(tmp_path)
        seqs.append(ledger.append(raw_event()).seq)
    assert seqs == [1, 2, 3, 4, 5]


# ------------------------------------------------------------------- 读取


def test_iter_returns_events_in_seq_order(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    # 故意让 timestamp 乱序：排序只认 seq，不认 timestamp
    batch = [
        raw_event(timestamp=NOW + timedelta(hours=3)),
        raw_event(timestamp=NOW - timedelta(hours=5)),
        raw_event(timestamp=NOW),
    ]
    stamped = ledger.append_many(batch)
    events = list(ledger.iter_events())
    assert [e.id for e in events] == [e.id for e in stamped]
    assert [e.seq for e in events] == [1, 2, 3]


def test_get_by_event_id(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    stamped = ledger.append_many([raw_event(), raw_event(), raw_event()])
    found = ledger.get(stamped[1].id)
    assert found is not None and found.seq == 2
    assert ledger.get("RAW-does-not-exist") is None


def test_iter_empty_and_missing_ledger(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    assert list(ledger.iter_events()) == []
    assert ledger.get("RAW-x") is None


def test_replay_api_filters(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append_many(
        [
            raw_event(task_id="TASK-a"),
            raw_event(task_id="TASK-b"),
            raw_event(task_id="TASK-a"),
        ]
    )
    ids = [e.id for e in iter_raw_events(tmp_path, task_id="TASK-a")]
    all_events = list(iter_raw_events(tmp_path))
    assert len(ids) == 2
    assert len(all_events) == 3
    assert [e.seq for e in iter_raw_events(tmp_path, project_id="PRJ-test")] == [1, 2, 3]
    assert list(iter_raw_events(tmp_path, project_id="PRJ-other")) == []


# --------------------------------------------------------------- 大规模写入


def test_10k_events_stable_write_and_read(tmp_path: Path) -> None:
    total = 10_000
    batch_size = 250
    ledger = RawEventLedger(tmp_path)
    stamped_all: list[RawEvent] = []
    for _ in range(total // batch_size):
        stamped_all.extend(ledger.append_many([raw_event() for _ in range(batch_size)]))

    events = list(ledger.iter_events())
    assert len(events) == total
    assert [e.seq for e in events] == list(range(1, total + 1))
    assert len({e.id for e in events}) == total
    assert events == stamped_all  # 完整 round-trip：读回的对象与写入副本一致

    report = ledger.check_integrity()
    assert report.ok
    assert report.valid_events == total


# ---------------------------------------------------------------- 批量原子性


def test_batch_failure_rolls_back_no_half_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append(raw_event())  # 已有 1 条

    def half_write_then_fail(self: RawEventLedger, blob: bytes) -> None:
        # 模拟写了一半崩溃
        with open(self.events_path, "ab") as fh:
            fh.write(blob[: len(blob) // 2])
            fh.flush()
        raise OSError("simulated disk failure")

    monkeypatch.setattr(RawEventLedger, "_write_blob_locked", half_write_then_fail)
    batch = [raw_event() for _ in range(4)]
    with pytest.raises(OSError, match="simulated disk failure"):
        ledger.append_many(batch)
    monkeypatch.undo()

    # 账本回滚到写入前状态：只有原来的 1 条
    events = list(ledger.iter_events())
    assert len(events) == 1
    # 后续写入从正确的 seq 继续（无空洞、无复用）
    assert ledger.append(raw_event()).seq == 2


# ----------------------------------------------------------------- 尾部损坏


def test_tail_corruption_identified_repaired(tmp_path: Path, caplog) -> None:
    ledger = RawEventLedger(tmp_path)
    stamped = ledger.append_many([raw_event(), raw_event()])
    good_bytes = ledger.events_path.read_bytes()
    partial = b'{"id":"RAW-xxx"'
    ledger.events_path.write_bytes(good_bytes + partial)

    reader = RawEventLedger(tmp_path)
    with caplog.at_level(logging.WARNING, logger="project_evolution.ledger.raw_ledger"):
        events = list(reader.iter_events())
    assert [e.id for e in events] == [e.id for e in stamped]  # 前两条仍可识别
    assert any("corrupt tail" in r.message for r in caplog.records)

    report = reader.check_integrity()
    assert not report.ok
    assert report.tail_corrupt
    assert any(issue.code == "LEDGER_TAIL_CORRUPTION" for issue in report.issues)

    # 追加被拒绝，直到显式 repair
    with pytest.raises(LedgerCorruptionError) as excinfo:
        reader.append(raw_event())
    assert excinfo.value.is_tail

    repair = reader.repair_tail()
    assert repair.status == "repaired"
    assert repair.truncated_bytes == len(partial)
    assert reader.events_path.read_bytes() == good_bytes
    assert reader.check_integrity().ok
    assert [e.id for e in reader.iter_events()] == [e.id for e in stamped]

    # repair 幂等：干净状态下再修是无操作
    again = reader.repair_tail()
    assert again.status == "clean"
    assert again.truncated_bytes == 0


def test_valid_json_without_trailing_newline_is_unconfirmed_tail(
    tmp_path: Path, caplog
) -> None:
    ledger = RawEventLedger(tmp_path)
    stamped = ledger.append(raw_event())
    good = ledger.events_path.read_bytes()
    # 追加一条完整 JSON 但没有换行符：append 从未确认过它
    unconfirmed = line_for(raw_event(), 2).rstrip(b"\n")
    ledger.events_path.write_bytes(good + unconfirmed)

    reader = RawEventLedger(tmp_path)
    with caplog.at_level(logging.WARNING, logger="project_evolution.ledger.raw_ledger"):
        events = list(reader.iter_events())
    assert [e.id for e in events] == [stamped.id]
    assert any("not newline-terminated" in r.message for r in caplog.records)

    report = reader.check_integrity()
    assert not report.ok and report.tail_corrupt
    assert report.valid_events == 1

    repair = reader.repair_tail()
    assert repair.status == "repaired"
    assert reader.events_path.read_bytes() == good


# ----------------------------------------------------------------- 中间损坏


def test_middle_corruption_raises_and_blocks_repair(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    e1, e2 = ledger.append_many([raw_event(), raw_event()])
    good1 = line_for(e1, 1)
    good2 = line_for(e2, 2)
    corrupt = b"this line is not json\n"
    ledger.events_path.write_bytes(good1 + corrupt + good2)

    reader = RawEventLedger(tmp_path)
    with pytest.raises(LedgerCorruptionError) as excinfo:
        list(reader.iter_events())
    assert not excinfo.value.is_tail
    assert excinfo.value.lineno == 2

    report = reader.check_integrity()
    assert not report.ok and not report.tail_corrupt
    assert any(issue.code == "INVALID_JSON" and issue.line == 2 for issue in report.issues)

    before = ledger.events_path.read_bytes()
    repair = reader.repair_tail()
    assert repair.status == "blocked"
    assert ledger.events_path.read_bytes() == before  # 绝不静默修改中间损坏


def test_append_refuses_middle_corruption(tmp_path: Path) -> None:
    ledger = RawEventLedger(tmp_path)
    e1 = ledger.append(raw_event())
    ledger.events_path.write_bytes(
        line_for(e1, 1) + b"{invalid}\n" + line_for(raw_event(), 2)
    )
    with pytest.raises(LedgerCorruptionError) as excinfo:
        RawEventLedger(tmp_path).append(raw_event())
    assert not excinfo.value.is_tail


# ------------------------------------------------------------ 记录级校验


def test_seq_violations_detected(tmp_path: Path) -> None:
    e1, e2, e3 = raw_event(), raw_event(), raw_event()
    path = tmp_path / ".project-evolution" / "events" / "raw.jsonl"
    path.parent.mkdir(parents=True)
    # seq 重复（1,2,2）
    path.write_bytes(line_for(e1, 1) + line_for(e2, 2) + line_for(e3, 2))
    ledger = RawEventLedger(tmp_path)
    with pytest.raises(LedgerCorruptionError, match="not strictly greater"):
        list(ledger.iter_events())
    report = ledger.check_integrity()
    assert any(issue.code == "DUPLICATE_SEQ" for issue in report.issues)
    assert not report.ok

    # seq 回退（1,3,2）
    path.write_bytes(line_for(e1, 1) + line_for(e2, 3) + line_for(e3, 2))
    with pytest.raises(LedgerCorruptionError, match="not strictly greater"):
        list(RawEventLedger(tmp_path).iter_events())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "SEQ_NOT_MONOTONIC" for issue in report.issues)

    # seq 跳跃（1,2,4）：warning 但 ok
    path.write_bytes(line_for(e1, 1) + line_for(e2, 2) + line_for(e3, 4))
    ledger = RawEventLedger(tmp_path)
    assert [e.seq for e in ledger.iter_events()] == [1, 2, 4]
    report = ledger.check_integrity()
    assert report.ok
    assert any(issue.code == "SEQ_GAP" and issue.severity == "warning" for issue in report.issues)


def test_duplicate_ids_in_file_detected(tmp_path: Path) -> None:
    e1 = raw_event(id="RAW-same")
    path = tmp_path / ".project-evolution" / "events" / "raw.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(line_for(e1, 1) + line_for(raw_event(id="RAW-same"), 2))
    ledger = RawEventLedger(tmp_path)
    report = ledger.check_integrity()
    assert not report.ok
    assert any(issue.code == "DUPLICATE_EVENT_ID" for issue in report.issues)


def test_bad_records_detected(tmp_path: Path) -> None:
    path = tmp_path / ".project-evolution" / "events" / "raw.jsonl"
    path.parent.mkdir(parents=True)
    base = raw_event().model_dump(mode="json")

    no_seq = dict(base)
    del no_seq["seq"]
    path.write_bytes((encode_record(no_seq) + "\n").encode())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "MISSING_SEQ" for issue in report.issues)

    newer = dict(base)
    newer["schema_version"] = 99
    path.write_bytes((encode_record(newer) + "\n").encode())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "UNSUPPORTED_SCHEMA_VERSION" for issue in report.issues)

    no_version = dict(base)
    del no_version["schema_version"]
    path.write_bytes((encode_record(no_version) + "\n").encode())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "SCHEMA_VERSION_MISSING" for issue in report.issues)

    bad_prefix = dict(base)
    bad_prefix["id"] = "X-123"
    path.write_bytes((encode_record(bad_prefix) + "\n").encode())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "BAD_ID_PREFIX" for issue in report.issues)

    broken_payload = dict(base)
    del broken_payload["source"]
    path.write_bytes((encode_record(broken_payload) + "\n").encode())
    report = RawEventLedger(tmp_path).check_integrity()
    assert any(issue.code == "INVALID_RECORD" for issue in report.issues)


def test_check_integrity_missing_file_is_ok(tmp_path: Path) -> None:
    report = RawEventLedger(tmp_path).check_integrity()
    assert report.ok
    assert not report.file_exists
    assert any(
        issue.code == "LEDGER_MISSING" and issue.severity == "warning" for issue in report.issues
    )


# ------------------------------------------------------------------- 并发


def test_concurrent_append_threads_serialized(tmp_path: Path) -> None:
    threads_count = 4
    per_thread = 100
    errors: list[Exception] = []

    def worker() -> None:
        try:
            ledger = RawEventLedger(tmp_path)
            for _ in range(per_thread):
                ledger.append(raw_event())
        except Exception as exc:  # noqa: BLE001 - 测试需捕获任意失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive()

    assert not errors
    ledger = RawEventLedger(tmp_path)
    events = list(ledger.iter_events())
    assert len(events) == threads_count * per_thread
    assert [e.seq for e in events] == list(range(1, threads_count * per_thread + 1))
    assert len({e.id for e in events}) == threads_count * per_thread
    assert ledger.check_integrity().ok
