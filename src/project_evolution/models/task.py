"""任务级领域对象：Task / TaskSessionLink / Checkpoint 与 Task 状态机。

字段与 docs/01-architecture/02-data-model.md §5/§6/§7 严格对齐；
状态机按 §5 的固定转换表实现，不自行扩展转换。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from project_evolution.errors import InvalidStateError, ValidationError

TaskStatus = Literal["active", "completed", "paused", "cancelled"]

TASK_STATUSES = ("active", "completed", "paused", "cancelled")

#: 固定转换表（02-data-model.md §5）。终态出边为空集合，禁止扩展。
ALLOWED_TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"active", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class Task(BaseModel):
    """工程演进的主要组织单元。"""

    id: str
    project_id: str
    title: str
    status: TaskStatus
    started_at: datetime
    finished_at: datetime | None = None
    milestone_id: str | None = None
    baseline_ref: str | None = None


class TaskSessionLink(BaseModel):
    """Task 与 Session 的多对多关系：一个 Task 可跨多个 Session，反之亦然。"""

    task_id: str
    session_id: str
    joined_at: datetime
    left_at: datetime | None = None


class Checkpoint(BaseModel):
    """长任务内的阶段性检查点；index 仅在同一个 Task 内递增。"""

    id: str
    project_id: str
    task_id: str
    session_id: str | None
    index: int
    reason: str | None = None
    summary: str | None = None
    created_at: datetime
    base_ref: str | None = None
    head_ref: str | None = None


def check_task_transition(current: str, target: str) -> None:
    """校验一次 Task 状态转换是否合法。

    规则（02-data-model.md §5，固定不变）：

    - ``active``/``paused`` 只能转向 paused/completed/cancelled（按表）；
    - ``completed`` 是终态，但重复 complete 幂等返回，不产生新事件；
    - ``cancelled`` 是终态，拒绝一切生命周期调用。

    :raises ValidationError: current/target 不是合法状态名。
    :raises InvalidStateError: 转换不在表中。
    """
    if current not in ALLOWED_TASK_TRANSITIONS:
        raise ValidationError(f"unknown task status {current!r}")
    if target not in ALLOWED_TASK_TRANSITIONS:
        raise ValidationError(f"unknown task status {target!r}")
    if current == "completed" and target == "completed":
        # 终态幂等：重复 complete 不产生新事件
        return
    if target not in ALLOWED_TASK_TRANSITIONS[current]:
        raise InvalidStateError(f"illegal task status transition: {current} -> {target}")
