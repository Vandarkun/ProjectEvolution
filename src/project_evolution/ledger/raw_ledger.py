"""RawEventLedger：``.project-evolution/events/raw.jsonl`` 的可靠追加与重放。

phase-05 起通用机制（锁、seq 分配、batch 原子性、尾部修复）抽取到
:class:`project_evolution.ledger.base.BaseJsonlLedger`，本类只绑定 RawEvent
的校验/序列化；对外行为与 phase-02 完全一致。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from project_evolution.config import PEL_DIR_NAME
from project_evolution.errors import ValidationError
from project_evolution.ledger.base import (
    BaseJsonlLedger,
    DuplicateEventError,
    LedgerCorruptionError,
)
from project_evolution.ledger.integrity import validate_raw_record
from project_evolution.models import RawEvent

__all__ = [
    "DuplicateEventError",
    "LedgerCorruptionError",
    "RawEventLedger",
    "iter_raw_events",
]


class RawEventLedger(BaseJsonlLedger[RawEvent]):
    """单项目的 raw.jsonl 账本。"""

    ledger_name = "raw"

    def __init__(
        self,
        project_root: str | Path,
        *,
        events_path: Path | None = None,
        lock_path: Path | None = None,
        fsync: bool = False,
        lock_timeout: float = 10.0,
    ):
        root = Path(project_root)
        super().__init__(
            events_path=(
                Path(events_path)
                if events_path is not None
                else root / PEL_DIR_NAME / "events" / "raw.jsonl"
            ),
            lock_path=(
                Path(lock_path)
                if lock_path is not None
                else root / PEL_DIR_NAME / ".runtime" / "locks" / "ledger.lock"
            ),
            fsync=fsync,
            lock_timeout=lock_timeout,
        )

    # ------------------------------------------------------------------ hooks

    @staticmethod
    def _validate_record(obj: dict) -> RawEvent:
        return validate_raw_record(obj)

    @staticmethod
    def _stamp_seq(event: RawEvent, seq: int) -> RawEvent:
        return event.model_copy(update={"seq": seq})

    @staticmethod
    def _serialize(event: RawEvent) -> dict:
        return event.model_dump(mode="json")

    def _validate_entry(self, event: RawEvent) -> None:
        if not isinstance(event, RawEvent):
            raise ValidationError(f"expected RawEvent, got {type(event).__name__}")


def iter_raw_events(
    project_root: str | Path,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    events_path: Path | None = None,
) -> Iterator[RawEvent]:
    """统一 Replay API（04-event-ledger.md §10）：按 seq 稳定顺序返回，可按过滤。"""
    ledger = RawEventLedger(project_root, events_path=events_path)
    for event in ledger.iter_events():
        if project_id is not None and event.project_id != project_id:
            continue
        if task_id is not None and event.task_id != task_id:
            continue
        yield event
