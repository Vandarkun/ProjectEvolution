"""JSONL 底层编码 / 解码 / 字节级扫描。

约定（docs/02-core/04-event-ledger.md §2-§3）：

- 一行一个完整 JSON Object，UTF-8，行尾 ``\\n``；
- 写入是紧凑单行（json.dumps 不产生换行）；
- 扫描按字节进行，保留精确 offset/长度，供尾部截断修复使用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class JSONLError(ValueError):
    """单行 JSON 解码失败（非法 JSON 或不是 JSON Object）。"""

    def __init__(self, message: str, *, lineno: int | None = None):
        super().__init__(message)
        self.lineno = lineno


@dataclass(frozen=True)
class LineScan:
    """一个物理行的字节级信息。

    ``start_offset`` 是行首字节偏移；``length`` 含行尾换行符（若存在）。
    """

    lineno: int
    start_offset: int
    length: int
    has_newline: bool
    raw: bytes


@dataclass(frozen=True)
class FileScan:
    size: int
    lines: list[LineScan]
    exists: bool = True


def encode_record(record: dict[str, Any]) -> str:
    """dict -> 单行紧凑 JSON 文本（不含换行）。调用方以 UTF-8 写入并补 ``\\n``。"""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def decode_line(text: str, *, lineno: int | None = None) -> dict[str, Any]:
    """单行文本 -> JSON Object。

    :raises JSONLError: 行不是合法 JSON，或不是 JSON Object。
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JSONLError(f"invalid JSON: {exc.msg}", lineno=lineno) from exc
    if not isinstance(obj, dict):
        raise JSONLError(f"record must be a JSON object, got {type(obj).__name__}", lineno=lineno)
    return obj


def decode_bytes(line: LineScan) -> tuple[str | None, str | None]:
    """行字节 -> (文本, 解码错误)。二者互斥；解码失败时文本为 None。"""
    try:
        return line.raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"invalid UTF-8: {exc.reason}"


def scan_file(path: Path) -> FileScan:
    """按字节扫描整个文件，切分为物理行并记录精确偏移。

    文件不存在时返回 ``exists=False`` 的空扫描（等价于空账本）。
    """
    if not path.exists():
        return FileScan(size=0, lines=[], exists=False)
    data = path.read_bytes()
    lines: list[LineScan] = []
    offset = 0
    lineno = 0
    while offset < len(data):
        lineno += 1
        nl = data.find(b"\n", offset)
        if nl == -1:
            chunk = data[offset:]
            lines.append(LineScan(lineno, offset, len(chunk), False, chunk))
            offset = len(data)
        else:
            chunk = data[offset : nl + 1]
            lines.append(LineScan(lineno, offset, len(chunk), True, chunk))
            offset = nl + 1
    return FileScan(size=len(data), lines=lines)
