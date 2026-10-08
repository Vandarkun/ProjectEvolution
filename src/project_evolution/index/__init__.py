"""Index 层（phase-06）：Canonical Ledger -> SQLite 投影 + 查询服务。"""

from project_evolution.index.store import ProjectionNotBuiltError, ProjectionStore

__all__ = [
    "ProjectionNotBuiltError",
    "ProjectionStore",
]
