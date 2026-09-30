"""Evolution 层：ChangeDigest 构建（phase-04）+ 语义分析（phase-05）。"""

from project_evolution.evolution.analyzer import (
    ANALYZER_VERSION,
    EVOLUTION_EVENT_TYPES,
    AnalyzerMode,
    AnalyzerRequest,
    AnalyzerResponse,
    CandidateEvent,
    CheckpointSummary,
    EvolutionEventSummary,
    EvolutionModelProvider,
    ProjectSummary,
    TaskSummary,
)
from project_evolution.evolution.change_digest import (
    ChangeDigest,
    ChangeDigestBuilder,
    DigestEvidence,
    SelectedHunk,
    privacy_exclusion,
)
from project_evolution.evolution.prompt import PROMPT_VERSION, load_system_prompt
from project_evolution.evolution.providers import (
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    parse_model_output,
)

__all__ = [
    "ANALYZER_VERSION",
    "EVOLUTION_EVENT_TYPES",
    "AnalyzerMode",
    "AnalyzerRequest",
    "AnalyzerResponse",
    "CandidateEvent",
    "ChangeDigest",
    "ChangeDigestBuilder",
    "CheckpointSummary",
    "DigestEvidence",
    "EvolutionEventSummary",
    "EvolutionModelProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "OllamaProvider",
    "PROMPT_VERSION",
    "ProjectSummary",
    "ProviderError",
    "SelectedHunk",
    "TaskSummary",
    "load_system_prompt",
    "parse_model_output",
    "privacy_exclusion",
]
