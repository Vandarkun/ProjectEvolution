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
from project_evolution.evolution.analyzer_service import (
    AnalysisError,
    AnalysisOutcome,
    EvolutionAnalyzerService,
    compute_analysis_key,
    digest_hash,
)
from project_evolution.evolution.change_digest import (
    ChangeDigest,
    ChangeDigestBuilder,
    DigestAgentSummary,
    DigestEvidence,
    DigestTestResult,
    SelectedHunk,
    privacy_exclusion,
)
from project_evolution.evolution.dedup import dedup_candidates, normalized_key
from project_evolution.evolution.prompt import PROMPT_VERSION, load_system_prompt
from project_evolution.evolution.providers import (
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    parse_model_output,
)
from project_evolution.evolution.validator import (
    Rejection,
    ValidationOutcome,
    ValidatorContext,
    validate_candidates,
)

__all__ = [
    "ANALYZER_VERSION",
    "EVOLUTION_EVENT_TYPES",
    "AnalysisError",
    "AnalysisOutcome",
    "AnalyzerMode",
    "AnalyzerRequest",
    "AnalyzerResponse",
    "CandidateEvent",
    "ChangeDigest",
    "ChangeDigestBuilder",
    "CheckpointSummary",
    "DigestAgentSummary",
    "DigestEvidence",
    "DigestTestResult",
    "EvolutionAnalyzerService",
    "EvolutionEventSummary",
    "EvolutionModelProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "OllamaProvider",
    "PROMPT_VERSION",
    "ProjectSummary",
    "ProviderError",
    "Rejection",
    "SelectedHunk",
    "TaskSummary",
    "ValidationOutcome",
    "ValidatorContext",
    "compute_analysis_key",
    "dedup_candidates",
    "digest_hash",
    "load_system_prompt",
    "normalized_key",
    "parse_model_output",
    "privacy_exclusion",
    "validate_candidates",
]
