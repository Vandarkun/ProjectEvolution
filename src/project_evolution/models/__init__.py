"""领域模型汇总导出。

全部模型与 docs/01-architecture/02-data-model.md 对齐。
"""

from project_evolution.models.events import EvolutionEvent, EvolutionEventType, RawEvent
from project_evolution.models.evidence import Evidence, EvidenceType
from project_evolution.models.evolution import (
    BugFix,
    Decision,
    FeatureChange,
    ParameterChange,
    ProjectState,
)
from project_evolution.models.project import Milestone, Project, Session, Version
from project_evolution.models.task import (
    ALLOWED_TASK_TRANSITIONS,
    TASK_STATUSES,
    Checkpoint,
    Task,
    TaskSessionLink,
    TaskStatus,
    check_task_transition,
)

__all__ = [
    "ALLOWED_TASK_TRANSITIONS",
    "TASK_STATUSES",
    "BugFix",
    "Checkpoint",
    "Decision",
    "Evidence",
    "EvidenceType",
    "EvolutionEvent",
    "EvolutionEventType",
    "FeatureChange",
    "Milestone",
    "ParameterChange",
    "Project",
    "ProjectState",
    "RawEvent",
    "Session",
    "Task",
    "TaskSessionLink",
    "TaskStatus",
    "Version",
    "check_task_transition",
]
