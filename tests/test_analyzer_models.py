"""Analyzer 请求/响应模型、Prompt 版本文件与 Provider 测试（phase-05 §3/§4/§7/§8）。

真实远端 Provider 不在默认 CI：unit 测试用注入 transport；
integration 测试由环境变量 opt-in（见 tests/test_providers_integration.py）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from project_evolution.evolution import (
    ANALYZER_VERSION,
    EVOLUTION_EVENT_TYPES,
    PROMPT_VERSION,
    AnalyzerRequest,
    AnalyzerResponse,
    CandidateEvent,
    CheckpointSummary,
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProjectSummary,
    ProviderError,
    TaskSummary,
    load_system_prompt,
    parse_model_output,
)
from project_evolution.evolution.analyzer import EvolutionEventSummary
from project_evolution.evolution.change_digest import ChangeDigest
from project_evolution.evolution.prompt import render_user_message


def make_request(**digest_overrides) -> AnalyzerRequest:
    digest = ChangeDigest(
        project_id="PRJ-test",
        task_id="TASK-1",
        checkpoint_id="CHK-1",
        changed_files=["Core/WindowOpacityService.cs"],
        summary="1 files changed",
        evidence=[
            {"id": "EVD-1", "type": "git_diff", "what": "Core/WindowOpacityService.cs"}
        ],
        **digest_overrides,
    )
    return AnalyzerRequest(
        project=ProjectSummary(project_id="PRJ-test", name="demo"),
        task=TaskSummary(task_id="TASK-1", title="增加窗口透明度"),
        checkpoint=CheckpointSummary(checkpoint_id="CHK-1", index=1, summary="核心逻辑完成"),
        digest=digest,
        relevant_history=[
            EvolutionEventSummary(
                event_id="EVO-1", type="decision", title="快捷键 Ctrl+Wheel", detail=""
            )
        ],
    )


# ------------------------------------------------------------------- 模型


def test_analyzer_request_roundtrip() -> None:
    request = make_request()
    data = request.model_dump(mode="json")
    assert data["mode"] == "checkpoint"
    restored = AnalyzerRequest.model_validate(data)
    assert restored.digest.changed_files == ["Core/WindowOpacityService.cs"]
    assert restored.relevant_history[0].event_id == "EVO-1"


def test_candidate_event_accepts_invalid_type_for_validator() -> None:
    """type 故意宽松：非法类型要能到达 Validator 出诊断（phase-05 §6）。"""
    event = CandidateEvent(type="not_a_type", title="x")
    assert event.type == "not_a_type"
    assert EVOLUTION_EVENT_TYPES == (
        "feature_change",
        "parameter_change",
        "decision",
        "bug_fix",
    )


def test_analyzer_response_allows_empty_events() -> None:
    response = AnalyzerResponse.model_validate({"events": []})
    assert response.events == []


def test_analyzer_version_is_pinned() -> None:
    assert ANALYZER_VERSION.startswith("pel-evolution-analyzer/")


# ------------------------------------------------------------------- prompt


def test_prompt_version_file_exists_and_loads() -> None:
    prompt = load_system_prompt()
    assert PROMPT_VERSION == "evolution-v1"
    assert "events" in prompt
    assert "feature_change" in prompt and "bug_fix" in prompt
    # §8 关键约束必须出现在 Prompt 中
    assert '"events": []' in prompt
    assert "evidence_ids" in prompt


def test_render_user_message_excludes_privacy_excluded() -> None:
    request = make_request()
    request.digest.excluded = [{"path": ".env", "reason": "secret"}]
    message = render_user_message(request.model_dump(mode="json"))
    assert "excluded" not in message
    assert ".env" not in message
    assert "EVD-1" in message


# ------------------------------------------------------------------- mock


def test_mock_provider_returns_fixed_events() -> None:
    fixed = AnalyzerResponse(
        events=[
            CandidateEvent(
                type="parameter_change",
                title="OpacityMin 0.2 -> 0.3",
                evidence_ids=["EVD-1"],
                payload={"name": "OpacityMin", "before": "0.2", "after": "0.3"},
            )
        ]
    )
    provider = MockProvider(responses=[fixed])
    response = asyncio.run(provider.analyze(make_request()))
    assert response.events[0].payload["name"] == "OpacityMin"


def test_mock_provider_default_is_noop() -> None:
    provider = MockProvider()
    response = asyncio.run(provider.analyze(make_request()))
    assert response.events == []


def test_mock_provider_responder_mode() -> None:
    provider = MockProvider(
        responder=lambda request: AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="decision",
                    title=f"decision for {request.checkpoint.checkpoint_id}",
                    evidence_ids=["EVD-1"],
                    payload={"subject": "s", "decision": "d"},
                )
            ]
        )
    )
    response = asyncio.run(provider.analyze(make_request()))
    assert response.events[0].title == "decision for CHK-1"


# ------------------------------------------------------------------- parsing


def test_parse_model_output_plain_and_fenced() -> None:
    plain = parse_model_output('{"events": []}')
    assert plain.events == []
    fenced = parse_model_output('```json\n{"events": []}\n```')
    assert fenced.events == []
    noisy = parse_model_output('Here you go:\n{"events": [{"type": "decision"}]} thanks')
    assert noisy.events[0].type == "decision"
    assert noisy.prompt_version == PROMPT_VERSION
    assert noisy.raw_output.startswith("Here you go")


def test_parse_model_output_rejects_garbage() -> None:
    with pytest.raises(ProviderError) as excinfo:
        parse_model_output("I think the changes are fine, no JSON")
    assert excinfo.value.kind == "invalid_output"
    with pytest.raises(ProviderError):
        parse_model_output('{"events": "yes"}')


# ------------------------------------------------------- providers (stubbed)


def stub_transport(status: int, payload: dict):
    calls: list[tuple[str, dict]] = []

    def transport(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, str]:
        calls.append((url, body))
        return status, json.dumps(payload)

    return transport, calls


def test_openai_provider_request_shape_and_parse() -> None:
    payload = {
        "choices": [
            {"message": {"content": json.dumps({"events": [{"type": "bug_fix"}]})}}
        ]
    }
    transport, calls = stub_transport(200, payload)
    provider = OpenAICompatibleProvider(
        model="gpt-mini", base_url="https://api.example.com/v1", api_key="sk-x", transport=transport
    )
    response = asyncio.run(provider.analyze(make_request()))
    assert response.events[0].type == "bug_fix"
    url, body = calls[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert body["model"] == "gpt-mini"
    assert body["stream"] is False
    assert body["messages"][0]["role"] == "system"
    assert PROMPT_VERSION in body["messages"][0]["content"]
    assert "EVD-1" in body["messages"][1]["content"]
    assert ".env" not in body["messages"][1]["content"]


def test_openai_provider_api_error() -> None:
    transport, _ = stub_transport(500, {"error": "boom"})
    provider = OpenAICompatibleProvider(
        model="m", base_url="https://api.example.com/v1", transport=transport
    )
    with pytest.raises(ProviderError, match="HTTP 500") as excinfo:
        asyncio.run(provider.analyze(make_request()))
    assert excinfo.value.kind == "api"


def test_ollama_provider_request_shape_and_parse() -> None:
    payload = {"message": {"content": '{"events": []}'}}
    transport, calls = stub_transport(200, payload)
    provider = OllamaProvider(
        model="llama3", base_url="http://localhost:11434", transport=transport
    )
    response = asyncio.run(provider.analyze(make_request()))
    assert response.events == []
    url, body = calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["format"] == "json"
    assert body["stream"] is False


def test_transport_timeout_maps_to_provider_error() -> None:
    def transport(url, body, headers, timeout):
        raise TimeoutError("timed out")

    provider = OllamaProvider(model="llama3", transport=transport)
    with pytest.raises(ProviderError, match="timeout") as excinfo:
        asyncio.run(provider.analyze(make_request()))
    assert excinfo.value.kind == "timeout"


# ------------------------------------------------- providers over real HTTP


def test_openai_provider_over_real_local_http() -> None:
    """真实 HTTP 栈（localhost 服务器 + urllib transport）端到端验证请求/响应处理。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 约定
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            self.server.request_bodies.append(body)  # type: ignore[attr-defined]
            content = json.dumps({"events": [{"type": "decision", "title": "t"}]})
            payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # 静默测试服务器日志
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.request_bodies = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAICompatibleProvider(
            model="local-mini", base_url=f"http://127.0.0.1:{server.server_port}/v1"
        )
        response = asyncio.run(provider.analyze(make_request()))
        assert response.events[0].type == "decision"
        sent = server.request_bodies[0]  # type: ignore[index]
        assert sent["model"] == "local-mini"
        assert sent["messages"][1]["role"] == "user"
    finally:
        server.shutdown()
        server.server_close()
