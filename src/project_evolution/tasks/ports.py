"""Task 服务的扩展端口（接口占位）。

phase-03 §5：Git baseline 此阶段只保留接口占位，Phase 04 才真正实现。
本模块只定义协议与数据形状，不包含任何 Git 逻辑。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from project_evolution.models import Evidence, RawEvent


class TaskBaseline(BaseModel):
    """start_task 时采集的基线（Phase 03 恒为空值占位，Phase 04 由 Git 填充）。"""

    head_ref: str | None = None
    branch: str | None = None
    #: Task 开始前已存在的 Dirty 变更路径（排除 .project-evolution/）
    status_paths: list[str] = Field(default_factory=list)
    #: 工作区快照文件（相对 .project-evolution/ 的路径）
    snapshot_path: str | None = None
    #: 非 Git 仓库或采集失败时的降级标记
    degraded: bool = False


class CheckpointEvidence(BaseModel):
    """checkpoint 时采集到的证据集合（Phase 03 恒为空占位，Phase 04 由 Git 填充）。"""

    base_ref: str | None = None
    head_ref: str | None = None
    snapshot_path: str | None = None
    diff_evidence: Evidence | None = None
    #: 本 checkpoint 增量产生的 changed-file RawEvents（file_added/modified/deleted），
    #: 由 CheckpointService 在 checkpoint_created 之后统一追加。
    file_events: list[RawEvent] = Field(default_factory=list)
    #: 上一快照 HEAD → 当前 HEAD 之间触及项目范围的提交（06 §9 git_commit
    # RawEvents，归属当前 Task/Checkpoint；枚举失败时为空，不影响主流程）。
    commit_events: list[RawEvent] = Field(default_factory=list)
    #: 快照链缺失等无法可靠区分归属时必须显式标记，而不是假装精确
    scope_confidence: str = "normal"


class BaselineRecorder(Protocol):
    """start_task 的基线采集端口（Phase 04 实现：HEAD/branch/status/工作区快照）。"""

    def record_task_baseline(self, task_id: str) -> TaskBaseline: ...


class CheckpointCollector(Protocol):
    """create_checkpoint 的证据采集端口（Phase 04 实现：快照对比 + diff evidence）。"""

    def collect_checkpoint_evidence(
        self,
        *,
        task_id: str,
        checkpoint_id: str,
        checkpoint_index: int,
        session_id: str | None,
        source: str,
    ) -> CheckpointEvidence: ...
