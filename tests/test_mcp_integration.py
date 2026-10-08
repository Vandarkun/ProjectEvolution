"""MCP 集成测试（phase-07 §9/§11）。

用真实测试 Client（subprocess + stdio 管道）完整执行：

```text
initialize → tools/list
→ start_task → checkpoint_task → get_history → complete_task → get_project_state
```

以及协议安全验收：

- 连续 100 次 tool call 不破坏协议（stdout 每一行都是合法 JSON-RPC）；
- 工具业务错误是 isError=true + 稳定错误码，协议不被污染。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

PROTOCOL_VERSION = "2025-06-18"


class McpClient:
    """极简 MCP stdio 测试 Client（JSON-RPC 逐行）。"""

    def __init__(self, repo: Path):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "project_evolution.mcp",
                "serve",
                "--project",
                str(repo),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 0
        self._stderr: list[bytes] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for chunk in iter(self.process.stderr.readline, b""):
            self._stderr.append(chunk)

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", errors="replace")

    def send(self, method: str, params: dict | None = None, *, notification: bool = False):
        assert self.process.stdin is not None
        message: dict = {"jsonrpc": "2.0", "method": method}
        if not notification:
            self._next_id += 1
            message["id"] = self._next_id
        if params is not None:
            message["params"] = params
        line = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self.process.stdin.write(line + b"\n")
        self.process.stdin.flush()
        return message.get("id")

    def read(self) -> dict:
        assert self.process.stdout is not None
        raw = self.process.stdout.readline()
        if not raw:
            raise AssertionError(
                f"server closed stdout; stderr:\n{self.stderr_text()[-2000:]}"
            )
        return json.loads(raw.decode("utf-8"))  # stdout 每一行都必须是 JSON

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self.send(method, params)
        while True:
            response = self.read()
            if response.get("id") == request_id:
                return response

    def call_tool(self, name: str, arguments: dict) -> dict:
        response = self.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        assert "error" not in response, response
        return response["result"]

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path)
    service = repo / "Core" / "WindowOpacityService.cs"
    service.parent.mkdir()
    service.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.2;\n"
        "}\n",
        encoding="utf-8",
    )
    commit_all(repo, "init")
    return repo


@pytest.fixture()
def client(repo: Path) -> Iterator[McpClient]:
    client = McpClient(repo)
    yield client
    client.close()


def initialize(client: McpClient) -> None:
    response = client.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pel-test-client", "version": "0"},
        },
    )
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    client.send("notifications/initialized", notification=True)


def tool_result_payload(result: dict) -> dict:
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


# --------------------------------------------------------------------- closed


def test_full_task_loop_over_stdio(client: McpClient, repo: Path) -> None:
    initialize(client)

    # 发现工具
    listed = client.request("tools/list")["result"]["tools"]
    names = {tool["name"] for tool in listed}
    assert {
        "start_task",
        "checkpoint_task",
        "complete_task",
        "get_project_state",
        "get_history",
    } <= names

    # start_task
    started = tool_result_payload(
        client.call_tool("start_task", {"title": "增加窗口透明度", "agent": "codex"})
    )
    assert started["status"] == "active"
    assert started["baseline"]
    task_id = started["task_id"]

    # checkpoint_task：事实 + 证据保存即返回，分析 pending（默认异步）
    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.3;\n"
        "}\n",
        encoding="utf-8",
    )
    checkpoint = tool_result_payload(
        client.call_tool(
            "checkpoint_task",
            {
                "task_id": task_id,
                "summary": "OpacityMin 0.2 -> 0.3",
                "reason": "milestone",
                "tests": [{"command": "dotnet build", "status": "passed"}],
            },
        )
    )
    assert checkpoint["checkpoint_saved"] is True
    assert checkpoint["analysis_status"] == "pending"
    assert checkpoint["evidence"]["changed_files"] == ["Core/WindowOpacityService.cs"]

    # get_history(kind=task)：紧凑结果 + 事件 ID
    history = tool_result_payload(
        client.call_tool("get_history", {"kind": "task", "query": task_id})
    )
    detail = history["results"][0]
    assert detail["status"] == "active"
    assert [cp["checkpoint_id"] for cp in detail["checkpoints"]] == [
        checkpoint["checkpoint_id"]
    ]
    assert detail["tests_passed"] == 1

    # complete_task：最后增量 checkpoint + 关闭
    completed = tool_result_payload(
        client.call_tool("complete_task", {"task_id": task_id, "summary": "done"})
    )
    assert completed["status"] == "completed"
    assert completed["checkpoint_id"] and completed["checkpoint_id"] != checkpoint["checkpoint_id"]

    # get_project_state：无 active task、投影 in-sync
    state = tool_result_payload(client.call_tool("get_project_state", {}))
    assert state["active_tasks"] == []
    assert state["projection"]["in_sync"] is True

    # mock provider 的分析是 NOOP：analysis 不产生语义事件，但事实全部可见
    final_history = tool_result_payload(
        client.call_tool("get_history", {"kind": "task", "query": task_id})
    )
    assert final_history["results"][0]["status"] == "completed"
    assert len(final_history["results"][0]["checkpoints"]) == 2


# -------------------------------------------------------------- protocol gate


def test_100_consecutive_calls_do_not_break_protocol(client: McpClient, repo: Path) -> None:
    """phase-07 §7：连续 100 次 tool call 后协议仍然完好。"""
    initialize(client)
    start = tool_result_payload(client.call_tool("start_task", {"title": "压测"}))
    assert start["status"] == "active"

    ok = 0
    errors = 0
    for i in range(100):
        if i % 4 == 3:
            result = client.call_tool("get_history", {"kind": "task", "query": "TASK-404"})
            payload = tool_result_payload(result)
            assert result["isError"] is True
            assert payload["code"] == "TASK_NOT_FOUND"
            errors += 1
        else:
            result = client.call_tool("get_project_state", {})
            assert result["isError"] is False
            payload = tool_result_payload(result)
            assert payload["projection"]["in_sync"] is True
            ok += 1
    assert ok == 75 and errors == 25

    # 协议仍然健康：initialize 响应正常
    ping = client.request("ping")
    assert ping["result"] == {}


def test_protocol_errors_do_not_kill_server(client: McpClient) -> None:
    initialize(client)
    response = client.request("resources/list")  # 未实现方法
    assert response["error"]["code"] == -32601
    bad = client.request("tools/call", {"name": "nope", "arguments": {}})
    assert bad["error"]["code"] == -32602
    # 之后仍可正常调用
    result = client.call_tool("get_project_state", {})
    assert result["isError"] is False


def test_server_startup_requires_project_configuration(tmp_path: Path) -> None:
    """无 --project/--registry 且 cwd 无 .project-evolution：拒绝启动（不猜项目）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "project_evolution.mcp", "serve"],
        cwd=empty,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert b"no project configured" in proc.stderr
    assert proc.stdout == b""  # 拒绝时也不污染 stdout
