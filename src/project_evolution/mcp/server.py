"""MCP stdio Server（phase-07 §7 Protocol Safety，08 §12/§13）。

协议安全约定：

- stdout 只有协议消息（一行一条 JSON-RPC）；任何 handler 执行期间对
  stdout 的普通 print 都被重定向到 stderr（拦截第三方库输出）；
- logging 全部走 stderr（serve() 里显式配置，不依赖根 logger 默认行为）；
- 工具错误按 MCP 语义返回 ``result.isError=true`` + 稳定错误 JSON
  （code/message/recoverable），协议错误才用 JSON-RPC error；
- 不把 Python traceback 返回给 Agent：内部异常细节写日志，响应只含
  稳定错误码与可读 message。

请求处理是单线程顺序的（stdio 客户端逐条发消息）；后台语义分析 worker
在独立线程，绝不向协议线程抛异常。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, BinaryIO

from project_evolution import __version__
from project_evolution.mcp.errors import ToolError
from project_evolution.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MAX_MESSAGE_BYTES,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    decode_line,
    encode_message,
    is_notification,
    make_error,
    make_response,
    negotiate_protocol_version,
)
from project_evolution.mcp.service import PelToolService
from project_evolution.mcp.tools import TOOL_SPECS, register_tools

_LOG = logging.getLogger(__name__)

SERVER_INFO: dict[str, Any] = {
    "name": "project-evolution-ledger",
    "version": __version__,
}

INSTRUCTIONS = (
    "Project Evolution Ledger: call start_task when beginning a well-defined task; "
    "checkpoint_task at subgoal completion, design changes, or before context "
    "compaction; complete_task when done; get_history / get_project_state to recall "
    "past decisions, parameters and bugs; search_events for semantic recall when "
    "you don't know exact names (cross-language). Do not call tools every turn."
)


class PelMcpServer:
    """单连接 stdio MCP Server（initialize / tools/list / tools/call / ping）。"""

    def __init__(self, service: PelToolService):
        self._service = service
        self._handlers = register_tools(service)
        self._initialized = False

    # ------------------------------------------------------------- dispatch

    def handle_message(
        self, message: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """处理一条已解码的 JSON-RPC 消息；返回响应（notification 返回 None）。"""
        if isinstance(message, list):
            # MCP 2025-06-18 起不支持 batch；对旧客户端逐条处理，保持宽容
            responses = [r for item in message if (r := self._handle_single(item))]
            return responses or None
        return self._handle_single(message)

    def _handle_single(self, message: Any) -> dict[str, Any] | None:
        if isinstance(message, list):
            return make_error(None, INVALID_REQUEST, "nested batch is not allowed")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return make_error(
                None, INVALID_REQUEST, "request must be a JSON-RPC 2.0 object"
            )
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return make_error(request_id, INVALID_REQUEST, "missing 'method'")
        if is_notification(message):
            self._handle_notification(method, message.get("params"))
            return None
        try:
            result = self._dispatch(method, message.get("params"))
        except _ProtocolFailure as exc:
            return make_error(request_id, exc.code, exc.message, exc.data)
        except Exception:  # noqa: BLE001 - 协议层绝不崩溃；细节进日志
            _LOG.exception("unhandled error processing %s", method)
            return make_error(
                request_id,
                INTERNAL_ERROR,
                "internal server error; see server logs (stderr)",
            )
        return make_response(request_id, result)

    def _dispatch(self, method: str, params: Any) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            self._require_initialized()
            return {"tools": TOOL_SPECS}
        if method == "tools/call":
            self._require_initialized()
            return self._call_tool(params)
        raise _ProtocolFailure(
            METHOD_NOT_FOUND, f"method not found: {method}"
        )

    def _initialize(self, params: Any) -> dict[str, Any]:
        requested = None
        if isinstance(params, dict):
            version = params.get("protocolVersion")
            if isinstance(version, str):
                requested = version
        self._initialized = True
        return {
            "protocolVersion": negotiate_protocol_version(requested),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _require_initialized(self) -> None:
        if not self._initialized:
            # MCP 规范：未 initialize 就调用 -> 协议层错误
            raise _ProtocolFailure(INVALID_REQUEST, "server not initialized")

    @staticmethod
    def _handle_notification(method: str, params: Any) -> None:
        _LOG.debug("notification %s %s", method, params)

    def _call_tool(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _ProtocolFailure(INVALID_PARAMS, "tools/call params must be an object")
        name = params.get("name")
        if not isinstance(name, str):
            raise _ProtocolFailure(INVALID_PARAMS, "tools/call requires a tool 'name'")
        handler = self._handlers.get(name)
        if handler is None:
            raise _ProtocolFailure(
                INVALID_PARAMS, f"unknown tool: {name}", data={"known": list(self._handlers)}
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise _ProtocolFailure(INVALID_PARAMS, "'arguments' must be an object")
        # stdout 保护：handler 期间任何普通 print 都改道 stderr
        with contextlib.redirect_stdout(sys.stderr):
            try:
                result = handler(arguments)
            except ToolError as exc:
                _LOG.warning("tool %s failed: %s: %s", name, exc.code, exc.message)
                return {
                    "content": [
                        {"type": "text", "text": json.dumps(exc.to_payload(), ensure_ascii=False)}
                    ],
                    "isError": True,
                }
            except Exception:  # noqa: BLE001 - 不向 Agent 泄漏 traceback
                _LOG.exception("tool %s crashed", name)
                error = ToolError(
                    "INTERNAL_ERROR",
                    "internal error; see server logs (stderr)",
                    recoverable=True,
                )
                return {
                    "content": [
                        {"type": "text", "text": json.dumps(error.to_payload(), ensure_ascii=False)}
                    ],
                    "isError": True,
                }
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
            "isError": False,
        }

    # ------------------------------------------------------------------ loop

    def serve_forever(
        self,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> int:
        """stdio 主循环：读到 EOF 退出；返回退出码（0 正常）。

        stdout 只写协议行；任何响应写入失败（客户端已断开）按正常结束处理。
        """
        reader = stdin if stdin is not None else sys.stdin.buffer
        writer = stdout if stdout is not None else sys.stdout.buffer
        exit_code = 0
        try:
            for raw_line in reader:
                line = raw_line.strip()
                if not line:
                    continue
                response = self._process_line(line)
                if response is not None:
                    writer.write(encode_message(response))
                    writer.write(b"\n")
                    writer.flush()
        except BrokenPipeError:
            pass  # 客户端断开
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(Exception):
                self._service.shutdown()
        return exit_code

    def _process_line(
        self, line: bytes
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        if len(line) > MAX_MESSAGE_BYTES:
            return make_error(
                None, INVALID_REQUEST, f"message exceeds {MAX_MESSAGE_BYTES} bytes"
            )
        try:
            message = decode_line(line)
        except (UnicodeDecodeError, ValueError):
            return make_error(None, PARSE_ERROR, "invalid JSON-RPC message")
        return self.handle_message(message)


class _ProtocolFailure(Exception):
    """JSON-RPC 层错误（区别于工具业务错误）。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def configure_logging(level: int = logging.WARNING) -> None:
    """stdio 模式日志配置：一律 stderr，绝不触碰 stdout。"""
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
