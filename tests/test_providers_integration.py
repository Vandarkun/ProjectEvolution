"""真实 Provider 集成测试（phase-05 §7）—— 默认跳过，环境变量 opt-in。

CI 不跑真实模型（成本与不稳定性）；本地手动验证任选其一：

- OpenAI 兼容端点::

    PEL_TEST_OPENAI_BASE_URL=https://api.openai.com/v1 \\
    PEL_TEST_OPENAI_MODEL=gpt-4o-mini \\
    PEL_TEST_OPENAI_API_KEY=sk-... pytest tests/test_providers_integration.py

- Ollama 本地模型::

    PEL_TEST_OLLAMA_MODEL=qwen2.5:7b pytest tests/test_providers_integration.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from project_evolution.evolution import (
    AnalyzerRequest,
    AnalyzerResponse,
    CheckpointSummary,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProjectSummary,
    TaskSummary,
)
from project_evolution.evolution.change_digest import ChangeDigest, DigestEvidence

OPENAI_BASE = os.environ.get("PEL_TEST_OPENAI_BASE_URL")
OPENAI_MODEL = os.environ.get("PEL_TEST_OPENAI_MODEL")
OPENAI_KEY = os.environ.get("PEL_TEST_OPENAI_API_KEY")
OLLAMA_MODEL = os.environ.get("PEL_TEST_OLLAMA_MODEL")
OLLAMA_URL = os.environ.get("PEL_TEST_OLLAMA_URL", "http://localhost:11434")


def real_request() -> AnalyzerRequest:
    digest = ChangeDigest(
        project_id="PRJ-eval",
        task_id="TASK-eval",
        checkpoint_id="CHK-eval",
        changed_files=["Core/WindowOpacityService.cs"],
        diff_stat="1 files changed, 6 insertions(+), 2 deletions(-)",
        selected_hunks=[
            {
                "path": "Core/WindowOpacityService.cs",
                "header": "@@ -12,7 +12,7 @@",
                "content": (
                    " private const double OpacityMin = 0.2;\n"
                    "-private const double OpacityMin = 0.2;\n"
                    "+private const double OpacityMin = 0.3;\n"
                ),
            }
        ],
        summary="1 files changed, 6 insertions(+), 2 deletions(-)",
        evidence=[DigestEvidence(id="EVD-901", type="git_diff", what="WindowOpacityService.cs")],
    )
    return AnalyzerRequest(
        project=ProjectSummary(project_id="PRJ-eval", name="eval"),
        task=TaskSummary(task_id="TASK-eval", title="增加窗口透明度"),
        checkpoint=CheckpointSummary(
            checkpoint_id="CHK-eval", index=1, summary="把最低透明度从 0.2 提高到 0.3"
        ),
        digest=digest,
    )


pytestmark = pytest.mark.skipif(
    not (OPENAI_BASE and OPENAI_MODEL and OPENAI_KEY) and not OLLAMA_MODEL,
    reason="real-provider integration tests are opt-in via PEL_TEST_* env vars",
)


@pytest.mark.skipif(not (OPENAI_BASE and OPENAI_MODEL and OPENAI_KEY), reason="no openai config")
def test_openai_compatible_real_model() -> None:
    provider = OpenAICompatibleProvider(
        model=OPENAI_MODEL or "", base_url=OPENAI_BASE or "", api_key=OPENAI_KEY
    )
    response = asyncio.run(provider.analyze(real_request()))
    assert isinstance(response, AnalyzerResponse)
    # 真实模型最低要求：合法 Schema + 只引用清单内 Evidence
    for event in response.events:
        assert event.type in ("feature_change", "parameter_change", "decision", "bug_fix")
        assert set(event.evidence_ids) <= {"EVD-901"}


@pytest.mark.skipif(not OLLAMA_MODEL, reason="no ollama config")
def test_ollama_real_model() -> None:
    provider = OllamaProvider(model=OLLAMA_MODEL or "", base_url=OLLAMA_URL)
    response = asyncio.run(provider.analyze(real_request()))
    assert isinstance(response, AnalyzerResponse)
    for event in response.events:
        assert event.type in ("feature_change", "parameter_change", "decision", "bug_fix")
        assert set(event.evidence_ids) <= {"EVD-901"}
