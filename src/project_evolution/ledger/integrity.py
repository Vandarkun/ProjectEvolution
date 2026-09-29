"""Ledger 完整性检查与尾部修复分析。

读取每条记录时至少验证（docs/02-core/04-event-ledger.md §11）：
JSON 格式、schema_version、ID 前缀、required fields（由 Pydantic 模型保证）、
seq 存在；坏记录返回准确的文件、行号和 Event ID。

损坏判级（04-event-ledger.md §4）：
- 中间行非法（无论是否带换行）-> 严重损坏，绝不静默修复；
- 仅最后一行非法且文件不以换行结束 -> 尾部中断（未确认写入），可由 repair 截断；
- 最后一行是合法 JSON 但文件不以换行结束 -> 同样视为未确认写入（可截断），
  因为 append 只有在写入 ``JSON + \\n`` 并 flush 之后才向上层返回。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from project_evolution.ledger.jsonl import (
    FileScan,
    JSONLError,
    LineScan,
    decode_bytes,
    decode_line,
    scan_file,
)
from project_evolution.models import RawEvent

SUPPORTED_SCHEMA_VERSION = 1
RAW_EVENT_ID_PREFIX = "RAW-"


class LineValidationError(ValueError):
    """一行记录未通过 envelope 校验。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_raw_record(obj: dict) -> RawEvent:
    """校验并解析一条 raw ledger 记录。

    :raises LineValidationError: schema_version 缺失/不支持、字段非法、seq 缺失、
        ID 前缀错误等。code 标识具体问题。
    """
    if "schema_version" not in obj:
        raise LineValidationError("SCHEMA_VERSION_MISSING", "record has no schema_version")
    version = obj["schema_version"]
    if version != SUPPORTED_SCHEMA_VERSION:
        if isinstance(version, int) and version > SUPPORTED_SCHEMA_VERSION:
            raise LineValidationError(
                "UNSUPPORTED_SCHEMA_VERSION",
                f"schema_version={version} is newer than supported "
                f"{SUPPORTED_SCHEMA_VERSION}; explicit migration required",
            )
        raise LineValidationError(
            "UNSUPPORTED_SCHEMA_VERSION", f"invalid schema_version={version!r}"
        )
    try:
        event = RawEvent.model_validate(obj)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        raise LineValidationError(
            "INVALID_RECORD", f"invalid RawEvent at .{loc}: {first['msg']}"
        ) from exc
    if not event.id.startswith(RAW_EVENT_ID_PREFIX):
        raise LineValidationError(
            "BAD_ID_PREFIX", f"raw event id must start with {RAW_EVENT_ID_PREFIX!r}: {event.id!r}"
        )
    if event.seq is None:
        raise LineValidationError("MISSING_SEQ", "stored record must carry an assigned seq")
    return event


@dataclass(frozen=True)
class LineParse:
    """一行的解析结果：event 与 error 互斥。"""

    line: LineScan
    event: RawEvent | None
    error: LineValidationError | None
    decode_error: str | None = None

    @property
    def event_id(self) -> str | None:
        if self.event is not None:
            return self.event.id
        return None


def parse_scan(scan: FileScan) -> list[LineParse]:
    """把字节扫描结果解析为逐行验证结果（不做跨行检查）。"""
    parsed: list[LineParse] = []
    for line in scan.lines:
        text, decode_error = decode_bytes(line)
        if decode_error is not None:
            parsed.append(LineParse(line, None, None, decode_error))
            continue
        assert text is not None
        try:
            obj = decode_line(text, lineno=line.lineno)
            event = validate_raw_record(obj)
        except (JSONLError, LineValidationError) as exc:
            code = getattr(exc, "code", "INVALID_JSON")
            message = exc.args[0] if exc.args else str(exc)
            parsed.append(LineParse(line, None, LineValidationError(code, message)))
            continue
        parsed.append(LineParse(line, event, None))
    return parsed


class IntegrityIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    line: int | None = None
    event_id: str | None = None
    message: str


class IntegrityReport(BaseModel):
    ok: bool
    path: str
    file_exists: bool
    total_lines: int
    valid_events: int
    tail_corrupt: bool
    issues: list[IntegrityIssue] = Field(default_factory=list)


class RepairReport(BaseModel):
    status: Literal["clean", "repaired", "blocked"]
    truncated_bytes: int = 0
    original_size: int = 0
    new_size: int = 0
    message: str = ""


def check_integrity(path: Path) -> IntegrityReport:
    """检查 raw ledger 完整性：可解析性、ID 重复、seq 单调递增无重复、尾部损坏。"""
    issues: list[IntegrityIssue] = []
    scan = scan_file(path)
    if not scan.exists:
        return IntegrityReport(
            ok=True,
            path=str(path),
            file_exists=False,
            total_lines=0,
            valid_events=0,
            tail_corrupt=False,
            issues=[
                IntegrityIssue(
                    severity="warning",
                    code="LEDGER_MISSING",
                    message="ledger file does not exist yet (empty ledger)",
                )
            ],
        )

    parsed = parse_scan(scan)
    seen_ids: dict[str, int] = {}
    prev_seq: int | None = None
    valid_events = 0
    tail_corrupt = bool(scan.lines) and not scan.lines[-1].has_newline

    for item in parsed:
        line = item.line
        if item.decode_error is not None:
            issues.append(
                IntegrityIssue(
                    severity="error",
                    code="INVALID_UTF8",
                    line=line.lineno,
                    message=item.decode_error,
                )
            )
            continue
        if item.error is not None:
            issues.append(
                IntegrityIssue(
                    severity="error",
                    code=item.error.code,
                    line=line.lineno,
                    message=item.error.message,
                )
            )
            continue
        assert item.event is not None
        event = item.event
        # 未确认写入（最后一行无换行符）不计入有效事件，交由 repair 截断
        if line is scan.lines[-1] and not line.has_newline:
            continue
        if event.id in seen_ids:
            issues.append(
                IntegrityIssue(
                    severity="error",
                    code="DUPLICATE_EVENT_ID",
                    line=line.lineno,
                    event_id=event.id,
                    message=f"duplicate event id, first seen at line {seen_ids[event.id]}",
                )
            )
        else:
            seen_ids[event.id] = line.lineno
        assert event.seq is not None
        if prev_seq is not None:
            if event.seq == prev_seq:
                issues.append(
                    IntegrityIssue(
                        severity="error",
                        code="DUPLICATE_SEQ",
                        line=line.lineno,
                        event_id=event.id,
                        message=f"duplicate seq {event.seq}",
                    )
                )
            elif event.seq < prev_seq:
                issues.append(
                    IntegrityIssue(
                        severity="error",
                        code="SEQ_NOT_MONOTONIC",
                        line=line.lineno,
                        event_id=event.id,
                        message=f"seq {event.seq} is lower than previous seq {prev_seq}",
                    )
                )
            elif event.seq > prev_seq + 1:
                issues.append(
                    IntegrityIssue(
                        severity="warning",
                        code="SEQ_GAP",
                        line=line.lineno,
                        event_id=event.id,
                        message=f"seq jumped from {prev_seq} to {event.seq}",
                    )
                )
        prev_seq = event.seq
        valid_events += 1

    if tail_corrupt:
        issues.append(
            IntegrityIssue(
                severity="error",
                code="LEDGER_TAIL_CORRUPTION",
                line=scan.lines[-1].lineno,
                message="last line is not newline-terminated (unconfirmed write); "
                "run `pel doctor --repair` to truncate",
            )
        )

    ok = not any(issue.severity == "error" for issue in issues)
    return IntegrityReport(
        ok=ok,
        path=str(path),
        file_exists=True,
        total_lines=len(scan.lines),
        valid_events=valid_events,
        tail_corrupt=tail_corrupt,
        issues=issues,
    )


@dataclass(frozen=True)
class RepairPoint:
    """repair 分析结论：clean（无可修复）/ blocked（中间损坏，拒绝修改）/ repair（可截断）。"""

    status: Literal["clean", "repair", "blocked"]
    truncate_offset: int = 0
    lineno: int | None = None
    reason: str = ""


def find_repair_point(path: Path) -> RepairPoint:
    """判定尾部是否可安全截断。

    只有当"所有完整行（以换行结束）都合法，且问题仅出现在未以换行结束的最后一行"
    时才允许截断；任何中间坏行都拒绝修复（不静默修改中间损坏）。
    """
    scan = scan_file(path)
    if not scan.exists or not scan.lines:
        return RepairPoint("clean")
    if scan.lines[-1].has_newline:
        # 所有行都完整（以换行结束）：只有全部合法才是 clean；
        # 任何坏行（包括最后一行）都是完整损坏，拒绝由 tail repair 处理
        for item in parse_scan(scan):
            if item.decode_error is not None:
                detail = item.decode_error
            elif item.error is not None:
                detail = item.error.message
            else:
                continue
            return RepairPoint(
                "blocked",
                lineno=item.line.lineno,
                reason=(
                    f"corruption at complete line {item.line.lineno} (`{detail}`): "
                    "tail repair only truncates an unconfirmed tail"
                ),
            )
        return RepairPoint("clean")
    last = scan.lines[-1]
    for item in parse_scan(scan):
        if item.line is last:
            break
        if item.decode_error is not None:
            detail = item.decode_error
        elif item.error is not None:
            detail = item.error.message
        else:
            continue
        return RepairPoint(
            "blocked",
            lineno=item.line.lineno,
            reason=(
                f"middle corruption at line {item.line.lineno} "
                f"(`{detail}`): refusing to modify; tail repair only truncates "
                "an unconfirmed tail"
            ),
        )
    return RepairPoint("repair", truncate_offset=last.start_offset, lineno=last.lineno)
