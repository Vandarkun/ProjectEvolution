"""RawEventLedger：``.project-evolution/events/raw.jsonl`` 的可靠追加与重放。

保证（docs/02-core/04-event-ledger.md §3、phase-02 §6）：

- 一行一个完整 JSON，UTF-8，行尾 ``\\n``，追加后 flush（可选 fsync）；
- seq 在项目级写锁内从 1 单调递增分配、不可复用；调用方构造事件时 seq 留空；
- 同一项目同一时刻仅一个 writer（跨进程/跨线程文件锁）；
- batch 一次性写入，失败时回滚到写入前大小，绝不半写；
- 不重写整个文件，只追加或（repair 时）截断未确认尾部。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from project_evolution.config import PEL_DIR_NAME
from project_evolution.errors import StorageError, ValidationError
from project_evolution.ledger.integrity import (
    IntegrityReport,
    LineValidationError,
    RepairReport,
    check_integrity,
    find_repair_point,
    parse_scan,
    validate_raw_record,
)
from project_evolution.ledger.jsonl import (
    JSONLError,
    LineScan,
    decode_bytes,
    decode_line,
    encode_record,
    scan_file,
)
from project_evolution.ledger.lock import write_lock
from project_evolution.models import RawEvent

_LOG = logging.getLogger(__name__)


class LedgerCorruptionError(StorageError):
    """账本损坏。``is_tail=True`` 表示未确认的尾部中断（可 repair 截断）。"""

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        lineno: int | None = None,
        is_tail: bool = False,
    ):
        super().__init__(message)
        self.path = path
        self.lineno = lineno
        self.is_tail = is_tail


class DuplicateEventError(StorageError):
    """追加的事件 ID 已存在于账本（或同一 batch 内重复）。"""


class RawEventLedger:
    """单项目的 raw.jsonl 账本。"""

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
        self.project_root = root
        self._events_path = (
            Path(events_path)
            if events_path is not None
            else root / PEL_DIR_NAME / "events" / "raw.jsonl"
        )
        self._lock_path = (
            Path(lock_path)
            if lock_path is not None
            else root / PEL_DIR_NAME / ".runtime" / "locks" / "ledger.lock"
        )
        self._fsync = fsync
        self._lock_timeout = lock_timeout
        # 写侧增量索引：仅 append 持锁路径使用；读路径每次独立扫描文件
        self._index_offset: int | None = None
        self._ids: set[str] = set()
        self._next_seq = 1
        self._last_seq = 0
        self._line_count = 0

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    # ------------------------------------------------------------------ write

    def append(self, event: RawEvent, *, assume_locked: bool = False) -> RawEvent:
        """锁内分配 seq 并追加一行，返回带 seq 的事件副本（入参不被修改）。

        ``assume_locked=True`` 供上层服务在已持有项目写锁的临界区内追加
        （read-project-then-append 必须整体串行，锁不可重入，见 lock.py）。
        """
        return self.append_many([event], assume_locked=assume_locked)[0]

    def append_many(
        self, events: list[RawEvent], *, assume_locked: bool = False
    ) -> list[RawEvent]:
        """整批写入：验证通过后一次性追加，失败回滚，不产生半写。"""
        batch = list(events)
        for event in batch:
            if not isinstance(event, RawEvent):
                raise ValidationError(f"expected RawEvent, got {type(event).__name__}")
            if event.seq is not None:
                raise ValidationError(
                    f"event {event.id}: seq is assigned by the ledger, leave it empty (None)"
                )
        batch_ids = [event.id for event in batch]
        if len(set(batch_ids)) != len(batch_ids):
            duplicates = sorted({eid for eid in batch_ids if batch_ids.count(eid) > 1})
            raise DuplicateEventError(f"duplicate event id within batch: {duplicates}")

        if assume_locked:
            return self._append_batch_locked(batch, batch_ids)
        with write_lock(self._lock_path, timeout=self._lock_timeout):
            return self._append_batch_locked(batch, batch_ids)

    def _append_batch_locked(self, batch: list[RawEvent], batch_ids: list[str]) -> list[RawEvent]:
        """在已持有项目写锁的前提下执行整批追加。"""
        self._refresh_index_locked()
        for eid in batch_ids:
            if eid in self._ids:
                raise DuplicateEventError(f"event id already exists in ledger: {eid}")

        base = self._next_seq
        stamped = [event.model_copy(update={"seq": base + i}) for i, event in enumerate(batch)]
        blob = "".join(
            encode_record(event.model_dump(mode="json")) + "\n" for event in stamped
        ).encode("utf-8")
        prev_size = self._index_offset  # refresh 后等于文件大小
        assert prev_size is not None
        try:
            self._write_blob_locked(blob)
        except BaseException:
            self._rollback_locked(prev_size)
            raise

        self._ids.update(batch_ids)
        self._next_seq = base + len(batch)
        self._line_count += len(batch)
        if batch:
            self._last_seq = base + len(batch) - 1
        self._index_offset = prev_size + len(blob)
        return stamped

    # ------------------------------------------------------------------- read

    def iter_events(self) -> Iterator[RawEvent]:
        """严格按 seq 顺序重放事件。

        - seq 非严格递增 -> :class:`LedgerCorruptionError`；
        - 中间坏行 -> :class:`LedgerCorruptionError`（绝不静默跳过）；
        - 未以换行结束的最后一行（无论是否合法 JSON，均为未确认写入）：
          记录 warning 并停止重放，由 ``pel doctor --repair`` 截断。
        """
        scan = scan_file(self._events_path)
        total = len(scan.lines)
        prev_seq: int | None = None
        for idx, item in enumerate(parse_scan(scan)):
            line = item.line
            is_last = idx == total - 1
            if item.decode_error is not None or item.error is not None:
                if is_last and not line.has_newline:
                    _LOG.warning(
                        "raw ledger has an unconfirmed corrupt tail at %s line %s "
                        "(run `pel doctor --repair`); replaying %s confirmed event(s) before it",
                        self._events_path,
                        line.lineno,
                        idx,
                    )
                    return
                if item.decode_error is not None:
                    code, detail = "INVALID_UTF8", item.decode_error
                else:
                    assert item.error is not None
                    code, detail = item.error.code, item.error.message
                raise LedgerCorruptionError(
                    f"{code}: {detail}", path=self._events_path, lineno=line.lineno, is_tail=False
                )
            assert item.event is not None
            if is_last and not line.has_newline:
                _LOG.warning(
                    "raw ledger last line %s at %s is valid JSON but not newline-terminated "
                    "(unconfirmed write, run `pel doctor --repair`); skipping it",
                    line.lineno,
                    self._events_path,
                )
                return
            seq = item.event.seq
            assert seq is not None
            if prev_seq is not None and seq <= prev_seq:
                raise LedgerCorruptionError(
                    f"seq {seq} is not strictly greater than previous seq {prev_seq}",
                    path=self._events_path,
                    lineno=line.lineno,
                    is_tail=False,
                )
            prev_seq = seq
            yield item.event

    def get(self, event_id: str) -> RawEvent | None:
        for event in self.iter_events():
            if event.id == event_id:
                return event
        return None

    def check_integrity(self) -> IntegrityReport:
        """完整性检查（含 seq 单调性与 ID 重复检查）。只读，不加写锁。"""
        return check_integrity(self._events_path)

    # ----------------------------------------------------------------- repair

    def repair_tail(self) -> RepairReport:
        """截断未确认的损坏尾部。

        只有"全部完整行合法、仅最后一行未以换行结束"时才截断；
        存在中间损坏时返回 ``blocked``，绝不静默修改。
        """
        with write_lock(self._lock_path, timeout=self._lock_timeout):
            point = find_repair_point(self._events_path)
            size = self._events_path.stat().st_size if self._events_path.exists() else 0
            if point.status == "clean":
                return RepairReport(
                    status="clean", original_size=size, new_size=size, message="no corrupt tail"
                )
            if point.status == "blocked":
                return RepairReport(
                    status="blocked", original_size=size, new_size=size, message=point.reason
                )
            with open(self._events_path, "r+b") as fh:
                fh.truncate(point.truncate_offset)
                fh.flush()
                os.fsync(fh.fileno())
            # 主动失效增量索引，下次写入前全量重扫
            self._index_offset = None
            truncated = size - point.truncate_offset
            return RepairReport(
                status="repaired",
                truncated_bytes=truncated,
                original_size=size,
                new_size=point.truncate_offset,
                message=(
                    f"truncated {truncated} unconfirmed tail byte(s) "
                    f"starting at line {point.lineno}"
                ),
            )

    # ----------------------------------------------------------------- inside

    def _write_blob_locked(self, blob: bytes) -> None:
        """把整批字节一次性追加到文件末尾并 flush（可选 fsync）。"""
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._events_path, "ab") as fh:
            fh.write(blob)
            fh.flush()
            if self._fsync:
                os.fsync(fh.fileno())

    def _reset_index(self) -> None:
        self._index_offset = 0
        self._ids = set()
        self._next_seq = 1
        self._last_seq = 0
        self._line_count = 0

    def _refresh_index_locked(self) -> None:
        """在持有写锁时同步增量索引到文件当前末尾。

        只扫描自上次已知 offset 之后的新增字节（其他 writer 追加的部分），
        保证 seq 分配跨实例/跨进程正确。检测到任何损坏都拒绝继续写入。
        """
        if (
            self._index_offset is None
            or not self._events_path.exists()
            or self._events_path.stat().st_size < self._index_offset
        ):
            self._reset_index()
        if not self._events_path.exists():
            return
        start = self._index_offset
        assert start is not None
        with open(self._events_path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()
        pos = 0
        while pos < len(blob):
            nl = blob.find(b"\n", pos)
            if nl == -1:
                raise LedgerCorruptionError(
                    "ledger has an unconfirmed partial tail; refusing to append "
                    "(run `pel doctor --repair`)",
                    path=self._events_path,
                    lineno=self._line_count + 1,
                    is_tail=True,
                )
            chunk = blob[pos : nl + 1]
            lineno = self._line_count + 1
            text, decode_error = decode_bytes(LineScan(lineno, 0, len(chunk), True, chunk))
            event: RawEvent | None = None
            code = "INVALID_UTF8"
            detail: str | None = decode_error
            if detail is None:
                assert text is not None
                try:
                    event = validate_raw_record(decode_line(text, lineno=lineno))
                except (JSONLError, LineValidationError) as exc:
                    code = getattr(exc, "code", "INVALID_JSON")
                    detail = exc.args[0] if exc.args else str(exc)
            if detail is not None or event is None:
                raise LedgerCorruptionError(
                    f"{code}: {detail}", path=self._events_path, lineno=lineno, is_tail=False
                )
            assert event is not None and event.seq is not None
            if event.id in self._ids:
                raise LedgerCorruptionError(
                    f"duplicate event id in ledger: {event.id}",
                    path=self._events_path,
                    lineno=lineno,
                    is_tail=False,
                )
            if event.seq <= self._last_seq:
                raise LedgerCorruptionError(
                    f"seq {event.seq} is not strictly greater than previous seq {self._last_seq}",
                    path=self._events_path,
                    lineno=lineno,
                    is_tail=False,
                )
            self._ids.add(event.id)
            self._last_seq = event.seq
            self._next_seq = event.seq + 1
            self._line_count += 1
            pos = nl + 1
        self._index_offset = start + pos

    def _rollback_locked(self, prev_size: int) -> None:
        """写入失败后把文件截回写入前大小，保证 batch 不半写。"""
        try:
            with open(self._events_path, "r+b") as fh:
                fh.truncate(prev_size)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise StorageError(
                "batch write failed AND rollback failed; ledger may have a partial tail, "
                f"run `pel doctor --repair`: {self._events_path}"
            ) from exc


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
