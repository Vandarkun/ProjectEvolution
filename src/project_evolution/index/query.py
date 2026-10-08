"""Query Service（phase-06 §6，10-timeline-and-query.md §3）。

把 SQLite 投影转化为人 / Coding Agent 关心的答案：

- Task / Parameter / Feature / Decision / Bug / File 历史与 Current State；
- 结果默认紧凑（10 §8：ID、时间、类型、标题、关键 before/after、Task、
  Evidence count），不内联 Diff；Evidence 通过 ``evidence_ids`` 按需下钻；
- Current State 一律由事件归约得到（10 §5）：
  Parameter 取最新有效 ``after``；Decision 应用 supersede；Feature 按
  added/modified/removed 归约。

本模块只读 SQLite 投影，不 import 任何 Provider / Analyzer（不直接调用 LLM），
也不写任何数据。MCP / FastAPI / CLI 的查询以后都复用这一层。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.index.projection import normalize_name
from project_evolution.index.store import ProjectionStore
from project_evolution.models import ProjectState

#: recent_bug_fixes 保留的条数（get_project_state）
RECENT_BUG_LIMIT = 10


# ------------------------------------------------------------------ models


class EvolutionItem(BaseModel):
    """紧凑语义事件条目（Progressive Disclosure：evidence_ids 供下钻）。"""

    event_id: str
    seq: int
    type: str
    title: str
    detail: str
    timestamp: datetime
    task_id: str | None
    evidence_count: int
    evidence_ids: list[str] = Field(default_factory=list)


class CheckpointItem(BaseModel):
    checkpoint_id: str
    index: int
    created_at: datetime
    summary: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    evidence_id: str | None = None


class TaskBrief(BaseModel):
    task_id: str
    title: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class TaskHistory(BaseModel):
    """Task Detail：生命周期 + Checkpoint + 文件 + 语义事件 + 测试概况。"""

    task_id: str
    title: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    session_ids: list[str] = Field(default_factory=list)
    checkpoints: list[CheckpointItem] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    file_change_count: int = 0
    evolution: list[EvolutionItem] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0


class ParameterItem(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    scope: str | None = None
    before: str | None = None
    after: str | None = None
    unit: str | None = None
    reason: str | None = None
    task_id: str | None = None


class ParameterHistory(BaseModel):
    name: str
    name_norm: str
    scope: str | None = None
    changes: list[ParameterItem] = Field(default_factory=list)
    current: str | None = None
    current_by_scope: dict[str, str] = Field(default_factory=dict)


class FeatureItem(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    action: str
    before: str | None = None
    after: str | None = None
    scope: str | None = None
    task_id: str | None = None


class FeatureHistory(BaseModel):
    name: str
    name_norm: str
    changes: list[FeatureItem] = Field(default_factory=list)
    #: present / absent；从未出现过该 Feature 时为 None
    current: str | None = None


class DecisionItem(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    decision: str
    reason: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    active: bool = True
    task_id: str | None = None


class DecisionHistory(BaseModel):
    subject: str
    subject_norm: str
    decisions: list[DecisionItem] = Field(default_factory=list)
    current: DecisionItem | None = None


class BugItem(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    problem: str
    cause: str | None = None
    fix: str
    affected_scope: list[str] = Field(default_factory=list)
    verification: str | None = None
    task_id: str | None = None
    evidence_count: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class FileChangeItem(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    path: str
    old_path: str | None = None
    change: str
    task_id: str | None = None


class FileHistory(BaseModel):
    """File History：文件变化 + 影响它的 Task 与语义事件（10 §6）。

    rename 之后按旧名或新名查询都能命中（``old_path`` 索引）。
    """

    path: str
    renamed_from: list[str] = Field(default_factory=list)
    file_changes: list[FileChangeItem] = Field(default_factory=list)
    tasks: list[TaskBrief] = Field(default_factory=list)
    evolution: list[EvolutionItem] = Field(default_factory=list)


class FileSummary(BaseModel):
    """关键 Changed Files 汇总行（Evolution Report §8）。"""

    path: str
    change_count: int
    first_at: datetime
    last_at: datetime


class EvidenceRecord(BaseModel):
    """Evidence 投影行（Dashboard 下钻入口；内容仍在 Evidence Store）。"""

    evidence_id: str
    type: str
    ref: str
    path: str | None = None
    created_at: datetime
    task_id: str | None = None
    checkpoint_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class ProjectionStatus(BaseModel):
    """SQLite 与 Ledger head 的同步标记（07 §13）。"""

    version: int
    db_path: str | None = None
    raw_seq: int = 0
    evo_seq: int = 0
    ledger_raw_seq: int = 0
    ledger_evo_seq: int = 0
    stale: bool = False
    in_sync: bool = False


#: Timeline 的合法 kind（phase-08 §6 筛选：Task/Feature/Parameter/Decision/
#: Bug/Version + Checkpoint 折叠项；File Event 不进入 Timeline，10 §4）
TIMELINE_KINDS = ("task", "checkpoint", "feature", "parameter", "decision", "bug", "version")

#: evolution_events.type -> timeline kind
_TYPE_TO_KIND = {
    "feature_change": "feature",
    "parameter_change": "parameter",
    "decision": "decision",
    "bug_fix": "bug",
}


def _canonical_timeline_kind(kind: str) -> str:
    """接受短名与事件类型名（feature / feature_change 等），未知值报错。"""
    value = kind.strip().lower()
    if value in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[value]
    if value in TIMELINE_KINDS:
        return value
    raise ValueError(f"unknown timeline kind {kind!r}; expected one of {TIMELINE_KINDS}")


class TimelineItem(BaseModel):
    """统一 Timeline 条目（10-timeline-and-query.md §4）。

    混合 Task lifecycle / Checkpoint / Feature / Parameter / Decision /
    BugFix / Version；低层 File Event 不出现在 Timeline（只在 Task Detail
    展开）。``event_id`` 指向底层 Raw / Evolution 事件以便追溯。
    """

    timestamp: datetime
    kind: str
    title: str
    summary: str
    task_id: str | None
    event_id: str
    evidence_count: int


# ----------------------------------------------------------------- service


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _loads(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _evolution_item(row) -> EvolutionItem:
    payload = _loads(row["payload"], {})
    keys = {
        "parameter_change": ("name", "before", "after"),
        "feature_change": ("feature", "action"),
        "decision": ("subject", "decision"),
        "bug_fix": ("problem", "fix"),
    }
    parts = [
        f"{key}={payload[key]}" for key in keys.get(row["type"], ()) if key in payload
    ]
    evidence_ids = _loads(row["evidence_ids"], [])
    return EvolutionItem(
        event_id=row["event_id"],
        seq=row["seq"],
        type=row["type"],
        title=row["title"],
        detail="; ".join(parts),
        timestamp=_dt(row["timestamp"]),
        task_id=row["task_id"],
        evidence_count=len(evidence_ids),
        evidence_ids=list(evidence_ids),
    )


def _ledger_head_seq(path: Path) -> int:
    """读账本最后一行的 seq（只读尾部，不整文件重放）。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65_536))
            tail = fh.read()
    except OSError:
        return 0
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
            return int(record["seq"])
        except (ValueError, KeyError, UnicodeDecodeError):
            continue
    return 0


class QueryService:
    """项目历史查询 API：唯一入口，外部接口（MCP/CLI/Dashboard）以后都复用。"""

    def __init__(
        self,
        project_root: str | Path,
        *,
        config: PelConfig | None = None,
        store: ProjectionStore | None = None,
    ):
        self._root = Path(project_root)
        self._config = config if config is not None else load_config(self._root)
        self._store = (
            store
            if store is not None
            else ProjectionStore(self._root / PEL_DIR_NAME / self._config.runtime_dir)
        )

    @property
    def store(self) -> ProjectionStore:
        return self._store

    def _connect(self):
        """取当前版本连接（指针变化自动重建；未建库时明确报错）。"""
        return self._store.connection()

    # ---------------------------------------------------------------- status

    def status(self) -> ProjectionStatus:
        version = self._store.current_version()
        if version < 1:
            return ProjectionStatus(version=0)
        conn = self._connect()
        try:
            meta = {
                str(row["key"]): str(row["value"])
                for row in conn.execute("SELECT key, value FROM projection_meta")
            }
        finally:
            conn.close()
        raw_seq = int(meta.get("raw_seq", "0"))
        evo_seq = int(meta.get("evo_seq", "0"))
        events_dir = self._root / PEL_DIR_NAME / self._config.events_dir
        ledger_raw = _ledger_head_seq(events_dir / "raw.jsonl")
        ledger_evo = _ledger_head_seq(events_dir / "evolution.jsonl")
        return ProjectionStatus(
            version=version,
            db_path=str(self._store.db_path(version)),
            raw_seq=raw_seq,
            evo_seq=evo_seq,
            ledger_raw_seq=ledger_raw,
            ledger_evo_seq=ledger_evo,
            stale=meta.get("stale") == "1",
            in_sync=raw_seq >= ledger_raw and evo_seq >= ledger_evo,
        )

    # ------------------------------------------------------------------ task

    def list_active_tasks(self) -> list[TaskBrief]:
        """非终态 Task（active/paused），按启动时间升序（get_project_state 用）。"""
        conn = self._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT * FROM tasks WHERE status NOT IN ('completed', 'cancelled') "
                    "ORDER BY started_at, task_id"
                )
            )
        finally:
            conn.close()
        return [
            TaskBrief(
                task_id=row["task_id"],
                title=row["title"],
                status=row["status"],
                started_at=_dt(row["started_at"]),
                finished_at=_dt(row["finished_at"]) if row["finished_at"] else None,
            )
            for row in rows
        ]

    def list_tasks(self, *, status: str | None = None) -> list[TaskBrief]:
        """全部 Task 概要（可按状态过滤），按启动时间升序。"""
        conn = self._connect()
        try:
            if status is None:
                rows = list(conn.execute("SELECT * FROM tasks ORDER BY started_at, task_id"))
            else:
                rows = list(
                    conn.execute(
                        "SELECT * FROM tasks WHERE status = ? ORDER BY started_at, task_id",
                        (status,),
                    )
                )
        finally:
            conn.close()
        return [
            TaskBrief(
                task_id=row["task_id"],
                title=row["title"],
                status=row["status"],
                started_at=_dt(row["started_at"]),
                finished_at=_dt(row["finished_at"]) if row["finished_at"] else None,
            )
            for row in rows
        ]

    def get_task_history(self, task_id: str) -> TaskHistory | None:
        conn = self._connect()
        try:
            task = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return None
            checkpoints = [
                CheckpointItem(
                    checkpoint_id=row["checkpoint_id"],
                    index=row["checkpoint_index"],
                    created_at=_dt(row["created_at"]),
                    summary=row["summary"],
                    base_ref=row["base_ref"],
                    head_ref=row["head_ref"],
                    evidence_id=row["diff_evidence_id"],
                )
                for row in conn.execute(
                    "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_index",
                    (task_id,),
                )
            ]
            file_rows = list(
                conn.execute(
                    "SELECT path FROM file_changes WHERE task_id = ? ORDER BY seq",
                    (task_id,),
                )
            )
            tests = {
                str(row["kind"]): int(row["n"])
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM raw_events WHERE task_id = ? "
                    "AND kind IN ('test_passed', 'test_failed') GROUP BY kind",
                    (task_id,),
                )
            }
            evolution = [
                _evolution_item(row)
                for row in conn.execute(
                    "SELECT * FROM evolution_events WHERE task_id = ? ORDER BY seq",
                    (task_id,),
                )
            ]
            session_ids = [
                str(row["session_id"])
                for row in conn.execute(
                    "SELECT session_id FROM task_sessions WHERE task_id = ? "
                    "ORDER BY joined_at",
                    (task_id,),
                )
            ]
        finally:
            conn.close()
        return TaskHistory(
            task_id=task_id,
            title=task["title"],
            status=task["status"],
            started_at=_dt(task["started_at"]),
            finished_at=_dt(task["finished_at"]) if task["finished_at"] else None,
            session_ids=session_ids,
            checkpoints=checkpoints,
            files=sorted({str(row["path"]) for row in file_rows}),
            file_change_count=len(file_rows),
            evolution=evolution,
            tests_passed=tests.get("test_passed", 0),
            tests_failed=tests.get("test_failed", 0),
        )

    # ------------------------------------------------------------- parameter

    def get_parameter_history(self, name: str, *, scope: str | None = None) -> ParameterHistory:
        """参数历史与当前值（10 §5：Current = 最新有效 after）。

        ``scope=None`` 表示不过滤作用域（跨作用域返回全部变化；
        ``current`` 为过滤范围内最新变化的 after）。
        """
        name_norm = normalize_name(name)
        sql = "SELECT * FROM parameter_changes WHERE name_norm = ?"
        params: list[object] = [name_norm]
        if scope is not None:
            sql += " AND scope IS ?"
            params.append(scope)
        sql += " ORDER BY seq"
        conn = self._connect()
        try:
            rows = list(conn.execute(sql, params))
            all_rows = list(
                conn.execute(
                    "SELECT scope, after_value FROM parameter_changes "
                    "WHERE name_norm = ? ORDER BY seq",
                    (name_norm,),
                )
            )
        finally:
            conn.close()
        changes = [
            ParameterItem(
                event_id=row["event_id"],
                seq=row["seq"],
                timestamp=_dt(row["timestamp"]),
                scope=row["scope"],
                before=row["before_value"],
                after=row["after_value"],
                unit=row["unit"],
                reason=row["reason"],
                task_id=row["task_id"],
            )
            for row in rows
        ]
        current_by_scope: dict[str, str] = {}
        for row in all_rows:
            if row["after_value"] is None:
                continue  # after 为空表示值未定义，不进入当前值
            current_by_scope[row["scope"] or ""] = row["after_value"]
        current: str | None = None
        if scope is not None:
            current = current_by_scope.get(scope)
        elif changes:
            current = changes[-1].after
        name_display = rows[-1]["name_display"] if rows else name
        return ParameterHistory(
            name=name_display,
            name_norm=name_norm,
            scope=scope,
            changes=changes,
            current=current,
            current_by_scope=current_by_scope,
        )

    # --------------------------------------------------------------- feature

    def get_feature_history(self, name: str) -> FeatureHistory:
        """Feature 历史；current 由 added/modified/removed 归约（10 §5）。"""
        name_norm = normalize_name(name)
        conn = self._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT * FROM feature_changes WHERE feature_name_norm = ? ORDER BY seq",
                    (name_norm,),
                )
            )
        finally:
            conn.close()
        changes = [
            FeatureItem(
                event_id=row["event_id"],
                seq=row["seq"],
                timestamp=_dt(row["timestamp"]),
                action=row["action"],
                before=row["before_desc"],
                after=row["after_desc"],
                scope=row["scope"],
                task_id=row["task_id"],
            )
            for row in rows
        ]
        current: str | None = None
        for row in rows:
            action = row["action"]
            if action in ("added", "modified"):
                current = "present"
            elif action == "removed":
                current = "absent"
        return FeatureHistory(
            name=rows[-1]["feature_display"] if rows else name,
            name_norm=name_norm,
            changes=changes,
            current=current,
        )

    # -------------------------------------------------------------- decision

    def get_decision_history(self, subject: str) -> DecisionHistory:
        """Decision 历史；应用 supersedes 链后得到 active/current（10 §5）。"""
        subject_norm = normalize_name(subject)
        conn = self._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT * FROM decisions WHERE subject_norm = ? ORDER BY seq",
                    (subject_norm,),
                )
            )
            # supersede 关系全局计算（被纠正对象可能属于其他 subject）
            all_supersedes: list[tuple[str, list[str]]] = [
                (str(row["event_id"]), [str(i) for i in _loads(row["supersedes"], [])])
                for row in conn.execute("SELECT event_id, supersedes FROM decisions")
            ]
        finally:
            conn.close()
        superseded = {target for _sid, supersedes in all_supersedes for target in supersedes}
        items = [
            DecisionItem(
                event_id=row["event_id"],
                seq=row["seq"],
                timestamp=_dt(row["timestamp"]),
                decision=row["decision"],
                reason=row["reason"],
                supersedes=[str(item) for item in _loads(row["supersedes"], [])],
                superseded_by=[
                    superseder_id
                    for superseder_id, supersedes in all_supersedes
                    if row["event_id"] in supersedes
                ],
                active=row["event_id"] not in superseded,
                task_id=row["task_id"],
            )
            for row in rows
        ]
        current = next(
            (item for item in reversed(items) if item.active), None
        )  # subject 内最新未被取代者
        return DecisionHistory(
            subject=rows[-1]["subject_display"] if rows else subject,
            subject_norm=subject_norm,
            decisions=items,
            current=current,
        )

    # ------------------------------------------------------------------- bug

    def get_bug_history(self, *, scope: str | None = None) -> list[BugItem]:
        """Bug 历史（时间正序）；``scope`` 过滤 affected_scope 命中的条目。"""
        conn = self._connect()
        try:
            rows = list(conn.execute("SELECT * FROM bug_fixes ORDER BY seq"))
            evidence_map = {
                str(row["event_id"]): _loads(row["evidence_ids"], [])
                for row in conn.execute(
                    "SELECT event_id, evidence_ids FROM evolution_events "
                    "WHERE type = 'bug_fix'"
                )
            }
        finally:
            conn.close()
        items = []
        for row in rows:
            affected = [str(item) for item in _loads(row["affected_scope"], [])]
            if scope is not None and scope not in affected:
                continue
            evidence_ids = list(evidence_map.get(str(row["event_id"]), []))
            items.append(
                BugItem(
                    event_id=row["event_id"],
                    seq=row["seq"],
                    timestamp=_dt(row["timestamp"]),
                    problem=row["problem"],
                    cause=row["cause"],
                    fix=row["fix"],
                    affected_scope=affected,
                    verification=row["verification"],
                    task_id=row["task_id"],
                    evidence_count=len(evidence_ids),
                    evidence_ids=evidence_ids,
                )
            )
        return items

    # ------------------------------------------------------------------ file

    def get_file_history(self, path: str) -> FileHistory:
        """File History：rename 后按旧名/新名均可继续追踪（10 §11）。

        匹配范围沿 rename 链扩展：查询任一曾用名 / 现名都得到完整历史。
        """
        conn = self._connect()
        try:
            names = self._rename_closure(conn, path)
            placeholders = ",".join("?" for _ in names)
            match = (
                f"path IN ({placeholders}) OR old_path IN ({placeholders})"
            )
            params = [*names, *names]
            file_rows = list(
                conn.execute(
                    f"SELECT * FROM file_changes WHERE {match} ORDER BY seq", params
                )
            )
            task_rows = list(
                conn.execute(
                    "SELECT DISTINCT t.* FROM tasks t JOIN file_changes f "
                    f"ON f.task_id = t.task_id WHERE f.{match} "
                    "ORDER BY t.started_at",
                    params,
                )
            )
            evolution_rows = list(
                conn.execute(
                    "SELECT e.* FROM evolution_events e WHERE e.checkpoint_id IN "
                    f"(SELECT checkpoint_id FROM file_changes WHERE {match}) "
                    "ORDER BY e.seq",
                    params,
                )
            )
        finally:
            conn.close()
        return FileHistory(
            path=path,
            renamed_from=sorted(
                {str(row["old_path"]) for row in file_rows if row["old_path"]} - {path}
            ),
            file_changes=[
                FileChangeItem(
                    event_id=row["event_id"],
                    seq=row["seq"],
                    timestamp=_dt(row["timestamp"]),
                    path=row["path"],
                    old_path=row["old_path"],
                    change=row["change"],
                    task_id=row["task_id"],
                )
                for row in file_rows
            ],
            tasks=[
                TaskBrief(
                    task_id=row["task_id"],
                    title=row["title"],
                    status=row["status"],
                    started_at=_dt(row["started_at"]),
                    finished_at=_dt(row["finished_at"]) if row["finished_at"] else None,
                )
                for row in task_rows
            ],
            evolution=[_evolution_item(row) for row in evolution_rows],
        )

    @staticmethod
    def _rename_closure(conn, path: str) -> list[str]:
        """沿 rename 边（old_path <-> path）求包含 ``path`` 的名字连通分量。"""
        adjacency: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT path, old_path FROM file_changes WHERE change = 'renamed' "
            "AND old_path IS NOT NULL"
        ):
            old, new = str(row["old_path"]), str(row["path"])
            adjacency.setdefault(old, set()).add(new)
            adjacency.setdefault(new, set()).add(old)
        component = {path}
        frontier = [path]
        while frontier:
            current = frontier.pop()
            for neighbor in adjacency.get(current, ()):
                if neighbor not in component:
                    component.add(neighbor)
                    frontier.append(neighbor)
        return sorted(component)

    # -------------------------------------------------------------- timeline

    def get_timeline(
        self,
        kinds: list[str] | tuple[str, ...] | set[str] | None = None,
        *,
        limit: int | None = None,
    ) -> list[TimelineItem]:
        """统一 Timeline（10 §4）：按时间正序混合各 kind 条目。

        - ``kinds=None`` 返回全部 kind；元素短名（``task``）或事件类型名
          （``feature_change``）均可，未知 kind 报 ``ValueError``；
        - ``version`` 在 V0.1 无 Version 事件，恒为空（与 get_history 一致）；
        - ``limit`` 取最新的 N 条（仍按时间正序返回）；
        - 低层 File Event 永不进入 Timeline（10 §4），只在 Task Detail 展开。
        """
        selected = None if kinds is None else {_canonical_timeline_kind(k) for k in kinds}

        def want(kind: str) -> bool:
            return selected is None or kind in selected

        items: list[TimelineItem] = []
        conn = self._connect()
        try:
            if want("task"):
                for row in conn.execute(
                    "SELECT e.event_id, e.timestamp, e.kind, e.task_id, t.title "
                    "FROM raw_events e JOIN tasks t ON t.task_id = e.task_id "
                    "WHERE e.kind IN ('task_started', 'task_completed')",
                ):
                    started = row["kind"] == "task_started"
                    items.append(
                        TimelineItem(
                            timestamp=_dt(row["timestamp"]),
                            kind="task",
                            title=(
                                f"Task started: {row['title']}"
                                if started
                                else f"Task completed: {row['title']}"
                            ),
                            summary="",
                            task_id=row["task_id"],
                            event_id=row["event_id"],
                            evidence_count=0,
                        )
                    )
            if want("checkpoint"):
                for row in conn.execute(
                    "SELECT e.event_id, e.timestamp, e.task_id, c.checkpoint_index, "
                    "c.summary, c.reason, c.diff_evidence_id "
                    "FROM raw_events e JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id "
                    "WHERE e.kind = 'checkpoint_created'"
                ):
                    summary_text = row["summary"] or row["reason"] or ""
                    items.append(
                        TimelineItem(
                            timestamp=_dt(row["timestamp"]),
                            kind="checkpoint",
                            title=f"Checkpoint #{row['checkpoint_index']}",
                            summary=summary_text,
                            task_id=row["task_id"],
                            event_id=row["event_id"],
                            evidence_count=1 if row["diff_evidence_id"] else 0,
                        )
                    )
            if want("feature") or want("parameter") or want("decision") or want("bug"):
                wanted_types = tuple(
                    event_type
                    for event_type, kind in _TYPE_TO_KIND.items()
                    if want(kind)
                )
                placeholders = ",".join("?" for _ in wanted_types)
                for row in conn.execute(
                    f"SELECT * FROM evolution_events WHERE type IN ({placeholders})",
                    wanted_types,
                ):
                    evidence_ids = _loads(row["evidence_ids"], [])
                    items.append(
                        TimelineItem(
                            timestamp=_dt(row["timestamp"]),
                            kind=_TYPE_TO_KIND[row["type"]],
                            title=row["title"],
                            summary=row["description"],
                            task_id=row["task_id"],
                            event_id=row["event_id"],
                            evidence_count=len(evidence_ids),
                        )
                    )
        finally:
            conn.close()
        items.sort(key=lambda item: (item.timestamp, item.event_id))
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be >= 1")
            items = items[-limit:]
        return items

    # ------------------------------------------------------------- summaries

    def list_changed_files(self, *, limit: int | None = None) -> list[FileSummary]:
        """按变更次数倒序的文件汇总（Evolution Report §8 关键 Changed Files）。"""
        conn = self._connect()
        try:
            rows = list(
                conn.execute(
                    "SELECT path, COUNT(*) AS n, MIN(timestamp) AS first_at, "
                    "MAX(timestamp) AS last_at FROM file_changes "
                    "GROUP BY path ORDER BY n DESC, path"
                )
            )
        finally:
            conn.close()
        files = [
            FileSummary(
                path=row["path"],
                change_count=row["n"],
                first_at=_dt(row["first_at"]),
                last_at=_dt(row["last_at"]),
            )
            for row in rows
        ]
        return files if limit is None else files[:limit]

    def list_parameter_histories(self, *, scope: str | None = None) -> list[ParameterHistory]:
        """全部参数的完整历史（Evolution Report §5 Parameter Evolution）。"""
        conn = self._connect()
        try:
            names = [
                str(row["name_display"])
                for row in conn.execute(
                    "SELECT name_display FROM parameter_changes "
                    "GROUP BY name_norm ORDER BY MIN(seq)"
                )
            ]
        finally:
            conn.close()
        return [self.get_parameter_history(name, scope=scope) for name in names]

    def list_feature_histories(self) -> list[FeatureHistory]:
        """全部 Feature 的完整历史（Evolution Report §4 Feature Evolution）。"""
        conn = self._connect()
        try:
            names = [
                str(row["feature_display"])
                for row in conn.execute(
                    "SELECT feature_display FROM feature_changes "
                    "GROUP BY feature_name_norm ORDER BY MIN(seq)"
                )
            ]
        finally:
            conn.close()
        return [self.get_feature_history(name) for name in names]

    def list_decision_histories(self) -> list[DecisionHistory]:
        """全部 Decision subject 的历史（Evolution Report §6 Decision History）。"""
        conn = self._connect()
        try:
            subjects = [
                str(row["subject_display"])
                for row in conn.execute(
                    "SELECT subject_display FROM decisions "
                    "GROUP BY subject_norm ORDER BY MIN(seq)"
                )
            ]
        finally:
            conn.close()
        return [self.get_decision_history(subject) for subject in subjects]

    # --------------------------------------------------------------- evidence

    def get_evidence_record(self, evidence_id: str) -> EvidenceRecord | None:
        """Evidence 投影行；不存在返回 None（内容文件不进 SQLite，07 §6）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            type=row["type"],
            ref=row["ref"],
            path=row["path"],
            created_at=_dt(row["created_at"]),
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            metadata=_loads(row["metadata"], {}),
        )

    # ---------------------------------------------------------- current state

    def get_project_state(self) -> ProjectState:
        """Current State（02-data-model.md §17）：全部由事件归约得到。"""
        conn = self._connect()
        try:
            feature_rows = list(
                conn.execute(
                    "SELECT feature_name_norm, feature_display, action "
                    "FROM feature_changes ORDER BY seq"
                )
            )
            parameter_rows = list(
                conn.execute(
                    "SELECT name_norm, name_display, after_value "
                    "FROM parameter_changes ORDER BY seq"
                )
            )
            decision_rows = list(
                conn.execute(
                    "SELECT event_id, subject_norm, subject_display, decision, supersedes "
                    "FROM decisions ORDER BY seq"
                )
            )
            bug_rows = list(
                conn.execute(
                    "SELECT problem FROM bug_fixes ORDER BY seq DESC LIMIT ?",
                    (RECENT_BUG_LIMIT,),
                )
            )
        finally:
            conn.close()

        present: dict[str, str] = {}
        for row in feature_rows:
            key = str(row["feature_name_norm"])
            if row["action"] == "removed":
                present.pop(key, None)
            else:
                present[key] = str(row["feature_display"])

        parameters: dict[str, str] = {}
        for row in parameter_rows:
            if row["after_value"] is None:
                continue
            parameters[str(row["name_display"])] = str(row["after_value"])

        superseded: set[str] = set()
        for row in decision_rows:
            superseded.update(str(item) for item in _loads(row["supersedes"], []))
        active: dict[str, str] = {}
        for row in decision_rows:
            if str(row["event_id"]) in superseded:
                continue
            active[str(row["subject_norm"])] = str(row["decision"])

        return ProjectState(
            active_features=sorted(present.values()),
            current_parameters=parameters,
            active_decisions=sorted(active.values()),
            recent_bug_fixes=[str(row["problem"]) for row in bug_rows],
            latest_version=None,  # V0.1 尚无 Version 事件，字段预留（02 §16/§17）
        )
