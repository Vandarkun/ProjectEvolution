"""Index 层（phase-06）：Canonical Ledger -> SQLite 投影 + 查询服务。

- :func:`rebuild_projection`：删除 DB 后可完整重建（按 seq 重放）；
- :class:`QueryService`：Task / Parameter / Feature / Decision / Bug /
  File / Current State 查询，全部只读 SQLite，不调用任何模型。
"""

from project_evolution.index.projection import (
    ProjectionError,
    ProjectionResult,
    RebuildReport,
    normalize_name,
    rebuild_projection,
)
from project_evolution.index.store import ProjectionNotBuiltError, ProjectionStore

__all__ = [
    "ProjectionError",
    "ProjectionNotBuiltError",
    "ProjectionResult",
    "ProjectionStore",
    "RebuildReport",
    "normalize_name",
    "rebuild_projection",
]
