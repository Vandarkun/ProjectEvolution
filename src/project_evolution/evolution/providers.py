"""Evolution Model Provider 实现（05-evolution-analyzer.md §7，phase-05 §3/§4/§7）。

- :class:`MockProvider`：固定返回构造时给定的事件（先证明闭环可靠），
  或用可注入的 responder 做确定性提取（离线评估 Fixture 用）；
- :class:`OpenAICompatibleProvider`：任何 OpenAI 兼容 chat/completions 端点；
- :class:`OllamaProvider`：本地 Ollama /api/chat（强制 JSON 输出）。

Provider 只负责模型调用与输出解析，不做 Ledger/Git/业务校验；
网络失败、超时、非法输出统一抛 :class:`ProviderError`（kind 区分原因，
由上层转入 pending 队列，phase-05 §9）。

真实 Provider 测试不进入默认 CI：unit 测试用注入 transport 模拟，
integration 测试由环境变量 opt-in（phase-05 §7）。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from project_evolution.errors import PelError
from project_evolution.evolution.analyzer import AnalyzerRequest, AnalyzerResponse
from project_evolution.evolution.prompt import (
    PROMPT_VERSION,
    load_system_prompt,
    render_user_message,
)

#: transport: (url, body, headers, timeout) -> (status, response_text)
Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]]

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


class ProviderError(PelError):
    """模型调用失败（网络/超时/API/输出不可解析）。kind: network|timeout|api|invalid_output。"""

    def __init__(self, message: str, *, kind: str = "network"):
        super().__init__(message)
        self.kind = kind


def urllib_transport(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, str]:
    """默认 stdlib HTTP transport（无第三方依赖，同步执行）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail[:500]}", kind="api") from exc
    except TimeoutError as exc:  # py3.10+ socket.timeout 即 TimeoutError
        raise ProviderError(f"timeout calling {url} after {timeout}s", kind="timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderError(
                f"timeout calling {url} after {timeout}s", kind="timeout"
            ) from exc
        raise ProviderError(f"cannot reach {url}: {exc.reason}", kind="network") from exc
    except OSError as exc:
        raise ProviderError(f"transport failure calling {url}: {exc}", kind="network") from exc


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON object：容忍 Markdown 代码块与前后杂文。

    :raises ProviderError: 找不到可解析的 JSON object。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise ProviderError(
        f"model output is not a JSON object: {stripped[:200]!r}", kind="invalid_output"
    )


def parse_model_output(text: str, *, model: str | None = None) -> AnalyzerResponse:
    """把模型文本输出解析为 AnalyzerResponse（events 非法结构 -> ProviderError）。

    prompt 契约要求顶层 ``{"events": [...]}``。缺失 ``events`` 键或值为 null
    属于模型违约（常见于服务端退化响应：空壳/包装对象/截断 JSON），按
    ``invalid_output`` 抛出进入 pending 可重试——静默当作 NOOP 会把这类
    响应伪装成健康的空结果（phase-09 真实项目验证中实际发生）。
    """
    obj = extract_json_object(text)
    events = obj.get("events")
    if events is None:
        raise ProviderError(
            f"'events' key missing or null in model output: {text[:200]!r}",
            kind="invalid_output",
        )
    if not isinstance(obj.get("events"), list):
        raise ProviderError(
            f"'events' must be a list, got {type(obj.get('events')).__name__}",
            kind="invalid_output",
        )
    try:
        return AnalyzerResponse.model_validate(
            {**obj, "model": model, "prompt_version": PROMPT_VERSION, "raw_output": text}
        )
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        raise ProviderError(
            f"event schema violation at .{'.'.join(str(p) for p in first['loc'])}: "
            f"{first['msg']}",
            kind="invalid_output",
        ) from exc


# ------------------------------------------------------------------- mock


class MockProvider:
    """固定事件 Provider（phase-05 §4）：不联网，供闭环与测试。

    - ``responses``：按序返回，耗尽后重复最后一个（None 视为 NOOP）；
    - ``responder``：按请求动态构造响应（离线评估/规则提取用）；
    - 二者都缺省时恒返回 ``events=[]``（NOOP 正常路径）。
    """

    def __init__(
        self,
        *,
        responses: list[AnalyzerResponse | None] | None = None,
        responder: Callable[[AnalyzerRequest], AnalyzerResponse | None] | None = None,
        name: str = "mock",
    ):
        self._responses = list(responses or [])
        self._responder = responder
        self._cursor = 0
        self.name = name

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerResponse:
        if self._responder is not None:
            response = self._responder(request)
            if response is not None:
                return response
        if not self._responses:
            return AnalyzerResponse(model=self.name, prompt_version=PROMPT_VERSION)
        index = min(self._cursor, len(self._responses) - 1)
        self._cursor += 1
        fixed = self._responses[index]
        return fixed or AnalyzerResponse(model=self.name, prompt_version=PROMPT_VERSION)


# ------------------------------------------------------------------- HTTP


class _HttpProviderMixin:
    """OpenAI 兼容 / Ollama 共用：渲染消息、线程内调用 transport、解析输出。"""

    model: str
    name: str
    timeout: float
    _url: str
    _transport: Transport

    def _chat(self, messages: list[dict[str, str]], extra_body: dict[str, Any]) -> AnalyzerResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            **extra_body,
        }
        try:
            status, text = self._transport(self._url, body, self._headers(), self.timeout)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                f"timeout calling {self._url} after {self.timeout}s", kind="timeout"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport 实现的异常统一归类，不向调用方泄漏
            raise ProviderError(
                f"transport failure calling {self._url}: {exc}", kind="network"
            ) from exc
        if status != 200:
            raise ProviderError(
                f"HTTP {status} from {self._url}: {text[:500]}", kind="api"
            )
        content = self._extract_content(json.loads(text))
        return parse_model_output(content, model=self.name)

    def _headers(self) -> dict[str, str]:
        return {}

    def _extract_content(self, data: dict[str, Any]) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(_HttpProviderMixin):
    """OpenAI 兼容 chat/completions Provider。

    ``base_url`` 形如 ``https://api.openai.com/v1``（POST ``{base_url}/chat/completions``）；
    API Key 只从外部注入，Provider 不落盘、不打日志。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        transport: Transport | None = None,
        json_mode: bool = False,
        name: str | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._transport = transport or urllib_transport
        self._json_mode = json_mode
        self._url = f"{self.base_url}/chat/completions"
        self.name = name or f"openai:{model}"

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerResponse:
        messages = [
            {"role": "system", "content": load_system_prompt()},
            {"role": "user", "content": render_user_message(request.model_dump(mode="json"))},
        ]
        extra: dict[str, Any] = {"temperature": 0}
        if self._json_mode:
            extra["response_format"] = {"type": "json_object"}
        return await asyncio.to_thread(self._chat, messages, extra)

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _extract_content(self, data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"unexpected chat/completions response shape: {str(data)[:300]}",
                kind="invalid_output",
            ) from exc
        if not isinstance(content, str):
            raise ProviderError("message.content is not a string", kind="invalid_output")
        return content


class OllamaProvider(_HttpProviderMixin):
    """本地 Ollama Provider（POST ``{base_url}/api/chat``，``format=json`` 强制 JSON）。

    默认 ``http://localhost:11434``；无 API Key。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: float = 300.0,
        transport: Transport | None = None,
        name: str | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or urllib_transport
        self._url = f"{self.base_url}/api/chat"
        self.name = name or f"ollama:{model}"

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerResponse:
        messages = [
            {"role": "system", "content": load_system_prompt()},
            {"role": "user", "content": render_user_message(request.model_dump(mode="json"))},
        ]
        return await asyncio.to_thread(
            self._chat, messages, {"format": "json", "options": {"temperature": 0}}
        )

    def _extract_content(self, data: dict[str, Any]) -> str:
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                f"unexpected /api/chat response shape: {str(data)[:300]}",
                kind="invalid_output",
            ) from exc
        if not isinstance(content, str):
            raise ProviderError("message.content is not a string", kind="invalid_output")
        return content
