"""Evolution Report / Overview 数据组装（phase-08，11-dashboard-and-export.md §7/§8）。

本模块只调用 :class:`~project_evolution.index.QueryService`（10 §3：MCP、
FastAPI、CLI 都调用查询层）；不读 JSONL、不连 SQLite、不解析 Git Diff。

关键结论携带底层 Event ID（11 §8：报告不是"模型凭空写的周报"）——
Event ID 直接来自 QueryService 结果对象，渲染层不得丢弃。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from project_evolution.git.evidence import DiffEvidenceStore
from project_evolution.index import (
    BugItem,
    DecisionHistory,
    EvidenceRecord,
    FeatureHistory,
    FileSummary,
    ParameterHistory,
    ProjectionStatus,
    ProvenanceSummary,
    QueryService,
    TaskBrief,
    TimelineItem,
)
from project_evolution.models import ProjectState

#: Overview "最近变化" 展示的 kind（11 §2：最近 Feature / Parameter / BugFix / Decision）
RECENT_KINDS = ("feature", "parameter", "bug", "decision")


class Overview(BaseModel):
    """Project Overview 页 / 报告 §1 的数据（11 §2）。"""

    project_name: str
    project_root: str
    latest_version: str | None
    task_total: int
    task_completed: int
    active_tasks: list[TaskBrief] = Field(default_factory=list)
    recent: dict[str, TimelineItem | None] = Field(default_factory=dict)
    time_start: datetime | None = None
    time_end: datetime | None = None
    current_state: ProjectState
    projection: ProjectionStatus


class EvolutionReport(BaseModel):
    """完整 Evolution Report（11 §7 的 10 节结构）。"""

    generated_at: datetime
    project_name: str
    project_root: str
    time_start: datetime | None = None
    time_end: datetime | None = None
    latest_version: str | None
    task_total: int
    task_completed: int
    active_tasks: list[TaskBrief] = Field(default_factory=list)
    tasks: list[TaskBrief] = Field(default_factory=list)
    features: list[FeatureHistory] = Field(default_factory=list)
    parameters: list[ParameterHistory] = Field(default_factory=list)
    decisions: list[DecisionHistory] = Field(default_factory=list)
    bugs: list[BugItem] = Field(default_factory=list)
    files: list[FileSummary] = Field(default_factory=list)
    current_state: ProjectState
    provenance: ProvenanceSummary
    projection: ProjectionStatus


def project_display_name(root: str | Path) -> str:
    return Path(root).name or str(root)


def build_overview(service: QueryService) -> Overview:
    """Project Overview（11 §2）：摘要 + 最近事件 + 当前状态。"""
    root = service.root
    tasks = service.list_tasks()
    active = [brief for brief in tasks if brief.status not in ("completed", "cancelled")]
    timeline = service.get_timeline()
    recent: dict[str, TimelineItem | None] = {
        kind: next((item for item in reversed(timeline) if item.kind == kind), None)
        for kind in RECENT_KINDS
    }
    state = service.get_project_state()
    return Overview(
        project_name=project_display_name(root),
        project_root=str(root),
        latest_version=state.latest_version,
        task_total=len(tasks),
        task_completed=sum(1 for brief in tasks if brief.status == "completed"),
        active_tasks=active,
        recent=recent,
        time_start=timeline[0].timestamp if timeline else None,
        time_end=timeline[-1].timestamp if timeline else None,
        current_state=state,
        projection=service.status(),
    )


def build_report(service: QueryService) -> EvolutionReport:
    """按 11 §7 的 10 节结构组装完整报告数据（渲染由 export 完成）。"""
    overview = build_overview(service)
    return EvolutionReport(
        generated_at=datetime.now(timezone.utc),
        project_name=overview.project_name,
        project_root=overview.project_root,
        time_start=overview.time_start,
        time_end=overview.time_end,
        latest_version=overview.latest_version,
        task_total=overview.task_total,
        task_completed=overview.task_completed,
        active_tasks=overview.active_tasks,
        tasks=service.list_tasks(),
        features=service.list_feature_histories(),
        parameters=service.list_parameter_histories(),
        decisions=service.list_decision_histories(),
        bugs=service.get_bug_history(),
        files=service.list_changed_files(),
        current_state=overview.current_state,
        provenance=service.get_provenance(),
        projection=overview.projection,
    )


def load_evidence_content(
    service: QueryService, evidence_id: str, *, max_chars: int = 20_000
) -> tuple[EvidenceRecord | None, str | None, bool]:
    """Evidence 下钻：投影元数据 + Evidence Store 的 Patch 纯文本。

    Patch 与 MCP get_evidence 相同上限（20k 字符）并按纯文本返回（11 §9）；
    记录不存在或文件缺失分别返回 None，调用方按 404 处理。
    """
    record = service.get_evidence_record(evidence_id)
    if record is None:
        return None, None, False
    if record.type != "git_diff":
        return record, None, False
    patch = DiffEvidenceStore(service.root).diffs_dir() / f"{evidence_id}.patch"
    if not patch.is_file():
        return record, None, False
    raw = patch.read_text(encoding="utf-8", errors="replace")
    truncated = len(raw) > max_chars
    return record, raw[:max_chars], truncated
