"""Evidence：语义事件可追溯的事实证据。

字段与 docs/01-architecture/02-data-model.md §9 严格对齐。
V0.1 不要求复制完整内容，可通过 ref 指向 diff 文件、RawEvent ID 或 Commit SHA。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceType = Literal["raw_event", "git_diff", "git_commit", "test_result", "file_snapshot"]


class Evidence(BaseModel):
    id: str
    project_id: str
    type: EvidenceType
    ref: str
    path: str | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
