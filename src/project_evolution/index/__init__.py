"""Index 层（phase-06）：Canonical Ledger -> SQLite 投影 + 查询服务。

- :func:`rebuild_projection`：删除 DB 后可完整重建（按 seq 重放）；
- :func:`update_projection`：增量追平（水位 + stale 标记）；
- :class:`QueryService`：Task / Parameter / Feature / Decision / Bug /
  File / Current State 查询，全部只读 SQLite，不调用任何模型。
"""

from project_evolution.index.projection import (
    ProjectionError,
    ProjectionResult,
    RebuildReport,
    UpdateReport,
    normalize_name,
    rebuild_projection,
    update_projection,
)
from project_evolution.index.query import (
    TIMELINE_KINDS,
    BugItem,
    DecisionHistory,
    DecisionItem,
    EvidenceRecord,
    EvolutionItem,
    FeatureHistory,
    FileHistory,
    FileSummary,
    ParameterHistory,
    ParameterItem,
    ProjectionStatus,
    QueryService,
    TaskHistory,
    TimelineItem,
)
from project_evolution.index.store import ProjectionNotBuiltError, ProjectionStore

__all__ = [
    "BugItem",
    "DecisionHistory",
    "DecisionItem",
    "EvidenceRecord",
    "EvolutionItem",
    "FeatureHistory",
    "FileHistory",
    "FileSummary",
    "ParameterHistory",
    "ParameterItem",
    "ProjectionError",
    "ProjectionNotBuiltError",
    "ProjectionResult",
    "ProjectionStatus",
    "ProjectionStore",
    "QueryService",
    "RebuildReport",
    "TIMELINE_KINDS",
    "TaskHistory",
    "TimelineItem",
    "UpdateReport",
    "normalize_name",
    "rebuild_projection",
    "update_projection",
]
