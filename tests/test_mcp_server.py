"""MCP stdio Server 协议测试（phase-07 §7 Protocol Safety，08 §12/§13）。

- initialize 版本协商、tools/list 发现、ping；
- notification 不产生响应；
- 未知 method / 未知工具 / 非法参数的稳定错误；
- 工具业务错误（isError=true + code/message/recoverable）与协议错误区分；
- stdout 纯净性：serve_forever 全程 stdout 每一行都是合法 JSON-RPC（含
  工具抛异常、第三方 print 污染尝试、100+ 次连续调用）。
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.mcp.errors import ToolError
from project_evolution.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    negotiate_protocol_version,
)
from project_evolution.mcp.registry import ProjectRegistry
from project_evolution.mcp.server import PelMcpServer
from project_evolution.mcp.service import PelToolService
from project_evolution.mcp.tools import tool_names


class FakeService:
    """只覆盖协议层所需行为的假 service。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str):
        def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
            self.calls.append((name, dict(arguments)))
            return {"ok": True, "tool": name}

        return handler

    start_task = property(lambda self: self._record("start_task"))
    checkpoint_task = property(lambda self: self._record("checkpoint_task"))
    complete_task = property(lambda self: self._record("complete_task"))
    get_project_state = property(lambda self: self._record("get_project_state"))
    get_history = property(lambda self: self._record("get_history"))

    def get_evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("evidence_id") == "boom":
            raise ToolError("EVIDENCE_NOT_FOUND", "evidence not found: boom")
        if arguments.get("evidence_id") == "crash":
            raise RuntimeError("secret traceback detail")
        return {"evidence_id": arguments.get("evidence_id")}

    def shutdown(self, *, timeout: float = 5.0) -> None:
        pass


def make_server() -> PelMcpServer:
    return PelMcpServer(FakeService())  # type: ignore[arg-type]


def call(server: PelMcpServer, method: str, params: Any = None, msg_id: Any = 1):
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
    )
    assert response is not None
    return response


@pytest.fixture()
def initialized() -> PelMcpServer:
    server = make_server()
    call(server, "initialize", {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]})
    return server


# ------------------------------------------------------------------ handshake


def test_initialize_negotiates_supported_version() -> None:
    server = make_server()
    response = call(server, "initialize", {"protocolVersion": "2024-11-05"})
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "project-evolution-ledger"
    assert result["instructions"]


def test_initialize_falls_back_to_latest_for_unknown_version() -> None:
    assert negotiate_protocol_version("1999-01-01") == SUPPORTED_PROTOCOL_VERSIONS[0]
    assert negotiate_protocol_version(None) == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_initialized_notification_has_no_response(initialized: PelMcpServer) -> None:
    assert (
        initialized.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )


def test_ping_returns_empty_object(initialized: PelMcpServer) -> None:
    assert call(initialized, "ping")["result"] == {}


# ----------------------------------------------------------------- discovery


def test_tools_list_exposes_six_tools_with_schemas(initialized: PelMcpServer) -> None:
    result = call(initialized, "tools/list")["result"]
    names = [tool["name"] for tool in result["tools"]]
    assert names == [
        "start_task",
        "checkpoint_task",
        "complete_task",
        "get_project_state",
        "get_history",
        "get_evidence",
    ]
    for tool in result["tools"]:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["required"], list)
    assert tool_names() == names


def test_tools_before_initialize_is_protocol_error() -> None:
    server = make_server()
    response = call(server, "tools/list")
    assert response["error"]["code"] == INVALID_REQUEST


# ------------------------------------------------------------------- calling


def test_tools_call_success_shape(initialized: PelMcpServer) -> None:
    result = call(
        initialized,
        "tools/call",
        {"name": "get_project_state", "arguments": {}},
    )["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "tool": "get_project_state"}


def test_tools_call_defaults_arguments_to_empty(initialized: PelMcpServer) -> None:
    result = call(initialized, "tools/call", {"name": "get_history"})["result"]
    assert result["isError"] is False
    service: FakeService = initialized._service  # type: ignore[assignment]
    assert service.calls[-1] == ("get_history", {})


def test_unknown_tool_is_invalid_params(initialized: PelMcpServer) -> None:
    response = call(initialized, "tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS
    assert "unknown tool" in response["error"]["message"]


def test_tool_error_maps_to_is_error_with_stable_code(initialized: PelMcpServer) -> None:
    result = call(
        initialized, "tools/call", {"name": "get_evidence", "arguments": {"evidence_id": "boom"}}
    )["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "EVIDENCE_NOT_FOUND",
        "message": "evidence not found: boom",
        "recoverable": True,
    }


def test_tool_crash_never_leaks_traceback(initialized: PelMcpServer) -> None:
    result = call(
        initialized, "tools/call", {"name": "get_evidence", "arguments": {"evidence_id": "crash"}}
    )["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "INTERNAL_ERROR"
    assert "secret traceback detail" not in json.dumps(result)
    assert "traceback" not in json.dumps(result).lower()


def test_unknown_method_is_method_not_found(initialized: PelMcpServer) -> None:
    response = call(initialized, "resources/list")
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_invalid_jsonrpc_object_rejected() -> None:
    server = make_server()
    response = server.handle_message({"id": 1, "method": "ping"})  # 缺 jsonrpc
    assert response["error"]["code"] == INVALID_REQUEST


# --------------------------------------------------------------- stdio purity


def test_stdio_every_stdout_line_is_jsonrpc(tmp_path: Path) -> None:
    """连续 120 次调用（含错误与污染尝试）：stdout 每一行都是合法 JSON-RPC。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("print('x')\n")
    commit_all(repo, "init")
    service = PelToolService(ProjectRegistry.single(repo), provider_factory=None)
    server = PelMcpServer(service)

    lines: list[bytes] = []
    lines.append(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]},
            }
        ).encode()
    )
    expected_ids: list[Any] = []
    for i in range(1, 121):
        if i % 5 == 0:
            name, args = "get_evidence", {"evidence_id": "EVD-missing"}
        elif i % 3 == 0:
            name, args = "get_project_state", {}
        else:
            name, args = "start_task", {"title": f"task {i}"} if i == 1 else {"title": ""}
        lines.append(
            json.dumps(
                {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                 "params": {"name": name, "arguments": args}}
            ).encode()
        )
        expected_ids.append(i)
    lines.append(b"{not json at all")  # parse error 行
    stdin = io.BytesIO(b"".join(line + b"\n" for line in lines))
    stdout = io.BytesIO()
    exit_code = server.serve_forever(stdin=stdin, stdout=stdout)
    assert exit_code == 0

    out_lines = stdout.getvalue().splitlines()
    assert len(out_lines) == 1 + 120 + 1  # initialize + 120 calls + parse error
    seen_ids = set()
    for raw in out_lines:
        message = json.loads(raw.decode("utf-8"))  # 每一行都必须是合法 JSON-RPC
        assert message["jsonrpc"] == "2.0"
        assert "id" in message
        if message["id"] != 0:
            seen_ids.add(message["id"])
    assert seen_ids == set(expected_ids) | {None}


def test_handler_print_does_not_pollute_stdout(tmp_path: Path) -> None:
    """handler 内的普通 print 必须被改道 stderr（08 §13 第三方库拦截）。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")
    service = PelToolService(ProjectRegistry.single(repo), provider_factory=None)
    original_start = service.start_task

    def noisy_start(arguments: Mapping[str, Any]) -> dict[str, Any]:
        print("NOISY LIBRARY OUTPUT", file=sys.stdout)  # 模拟第三方库 print
        return original_start(arguments)

    service.start_task = noisy_start  # type: ignore[method-assign]
    server = PelMcpServer(service)
    stdin = io.BytesIO(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        + b"\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "start_task", "arguments": {"title": "t"}},
            }
        ).encode()
        + b"\n"
    )
    stdout = io.BytesIO()
    server.serve_forever(stdin=stdin, stdout=stdout)
    for raw in stdout.getvalue().splitlines():
        message = json.loads(raw.decode("utf-8"))
        assert message["jsonrpc"] == "2.0"
    assert b"NOISY LIBRARY OUTPUT" not in stdout.getvalue()


def test_batched_messages_handled_elementwise(initialized: PelMcpServer) -> None:
    response = initialized.handle_message(
        [
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
        ]
    )
    assert isinstance(response, list)
    assert [item["id"] for item in response] == [10, 11]


def test_process_line_guards_oversized_messages(initialized: PelMcpServer) -> None:
    from project_evolution.mcp.protocol import MAX_MESSAGE_BYTES

    response = initialized._process_line(b"x" * (MAX_MESSAGE_BYTES + 1))
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST


def test_parse_error_response_shape(initialized: PelMcpServer) -> None:
    response = initialized._process_line(b"not-json{")
    assert response is not None
    assert response["error"]["code"] == PARSE_ERROR
