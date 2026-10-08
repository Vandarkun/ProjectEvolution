"""MCP stdio 协议基础（phase-07，08-mcp-integration.md §12/§13）。

MCP 层的协议约定：

- Transport 只用 stdio：一行一条 JSON-RPC 2.0 消息（UTF-8、无内嵌换行）；
- stdout 只允许协议消息；diagnostic 一律 stderr（见 server.py）；
- 本模块只做编解码与消息构造，不做任何业务逻辑。

支持的 MCP 协议版本（initialize 协商：客户端请求的版本受支持则原样回显，
否则回落到本实现最新版本，由客户端决定是否断开）。
"""

from __future__ import annotations

import json
from typing import Any

#: 按时间从新到旧排列；首个元素是本实现的最新版本
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

# ------------------------------------------------------------- JSON-RPC codes

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: 单行消息体积上限（防御性：超限行不可能是合法请求，直接按 parse error 处理）
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def negotiate_protocol_version(requested: str | None) -> str:
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return str(requested)
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def make_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def decode_line(line: bytes | str) -> Any:
    """解码一行 stdio 消息为 JSON 值。

    :raises ValueError: 不是合法 UTF-8 / 不是合法 JSON。
    """
    if isinstance(line, bytes):
        return json.loads(line.decode("utf-8"))
    return json.loads(line)


def encode_message(message: dict[str, Any]) -> bytes:
    """编码为单行协议消息（紧凑、无内嵌换行，末尾 ``\\n`` 由写入方补）。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def is_notification(message: Any) -> bool:
    """JSON-RPC notification：无 ``id`` 字段（显式 null 的 id 是请求，不是通知）。"""
    return isinstance(message, dict) and "id" not in message
