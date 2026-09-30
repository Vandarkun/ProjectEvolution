"""EvolutionEventLedger：``.project-evolution/events/evolution.jsonl``。

与 Raw Ledger 同样的稳定 append/read/replay（phase-05 §5"实现与 Raw Ledger
同样稳定的 append/read/replay"），并追加 Ledger 级幂等约束：

- 同一 ``analysis_batch_id`` 的事件整批提交（04-event-ledger.md §6）；
- ``analysis_key`` 已存在于账本（且属于其他 batch）时拒绝追加
  （03-event-model.md §12 幂等：同键已成功提交则返回已有结果，不重复写）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from project_evolution.config import PEL_DIR_NAME
from project_evolution.errors import StorageError, ValidationError
from project_evolution.ledger.base import (
    BaseJsonlLedger,
    DuplicateEventError,
    LedgerCorruptionError,
)
from project_evolution.ledger.integrity import validate_evolution_record
from project_evolution.models import EvolutionEvent

__all__ = [
    "DuplicateAnalysisKeyError",
    "DuplicateEventError",
    "EvolutionEventLedger",
    "LedgerCorruptionError",
    "iter_evolution_events",
]


class DuplicateAnalysisKeyError(StorageError):
    """追加的 analysis_key 已属于账本中的另一个成功批次。"""


class EvolutionEventLedger(BaseJsonlLedger[EvolutionEvent]):
    """单项目的 evolution.jsonl 账本。"""

    ledger_name = "evolution"

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
                else root / PEL_DIR_NAME / "events" / "evolution.jsonl"
            ),
            lock_path=(
                Path(lock_path)
                if lock_path is not None
                else root / PEL_DIR_NAME / ".runtime" / "locks" / "ledger.lock"
            ),
            fsync=fsync,
            lock_timeout=lock_timeout,
        )
        # analysis_key -> batch_id（已成功提交的批次）
        self._analysis_keys: dict[str, str] = {}

    # ------------------------------------------------------------------ hooks

    @staticmethod
    def _validate_record(obj: dict) -> EvolutionEvent:
        return validate_evolution_record(obj)

    @staticmethod
    def _stamp_seq(event: EvolutionEvent, seq: int) -> EvolutionEvent:
        return event.model_copy(update={"seq": seq})

    @staticmethod
    def _serialize(event: EvolutionEvent) -> dict:
        return event.model_dump(mode="json")

    def _validate_entry(self, event: EvolutionEvent) -> None:
        if not isinstance(event, EvolutionEvent):
            raise ValidationError(
                f"expected EvolutionEvent, got {type(event).__name__}"
            )

    def _reset_extra_index(self) -> None:
        self._analysis_keys = {}

    def _register_event(self, event: EvolutionEvent) -> None:
        self._analysis_keys[event.analysis_key] = event.analysis_batch_id

    def _check_batch_appendable(self, batch: list[EvolutionEvent]) -> None:
        super()._check_batch_appendable(batch)
        for event in batch:
            owner = self._analysis_keys.get(event.analysis_key)
            if owner is not None and owner != event.analysis_batch_id:
                raise DuplicateAnalysisKeyError(
                    f"analysis_key already committed in batch {owner}; "
                    "retry must reuse the existing batch (idempotency)"
                )

    # ------------------------------------------------------------------- read

    def find_batch(self, analysis_key: str) -> list[EvolutionEvent]:
        """返回该 analysis_key 已提交的全部事件（空列表表示未分析过）。"""
        return [event for event in self.iter_events() if event.analysis_key == analysis_key]

    def analysis_keys(self) -> set[str]:
        """账本中已成功提交的全部 analysis_key（幂等查询）。"""
        return {event.analysis_key for event in self.iter_events()}


def iter_evolution_events(
    project_root: str | Path,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    events_path: Path | None = None,
) -> Iterator[EvolutionEvent]:
    """统一 Replay API（04-event-ledger.md §10）：按 seq 稳定顺序返回，可按过滤。"""
    ledger = EvolutionEventLedger(project_root, events_path=events_path)
    for event in ledger.iter_events():
        if project_id is not None and event.project_id != project_id:
            continue
        if task_id is not None and event.task_id != task_id:
            continue
        yield event
