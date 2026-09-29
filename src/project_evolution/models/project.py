"""项目级领域对象：Project / Session / Milestone / Version。

字段与 docs/01-architecture/02-data-model.md §3/§4/§15/§16 严格对齐。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Project(BaseModel):
    """所有事件的顶级作用域。"""

    id: str
    name: str
    repository_root: str
    created_at: datetime
    schema_version: int
    git_remote: str | None = None
    default_branch: str | None = None


class Session(BaseModel):
    """一次 Coding Agent 会话；Session 不等于 Task。"""

    id: str
    project_id: str
    agent: str
    started_at: datetime
    ended_at: datetime | None = None
    external_session_id: str | None = None


class Milestone(BaseModel):
    """里程碑。V0.1 只支持显式创建，不做自动推断。"""

    id: str
    project_id: str
    title: str
    description: str | None = None
    status: Literal["planned", "active", "completed", "cancelled"]
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Version(BaseModel):
    """版本。优先映射 Git Tag，而不是自己实现版本控制。"""

    id: str
    project_id: str
    name: str
    git_tag: str | None = None
    git_commit: str | None = None
    created_at: datetime
