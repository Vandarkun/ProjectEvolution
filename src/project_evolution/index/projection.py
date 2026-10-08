"""Canonical Ledger -> SQLite 投影（phase-06 §4/§5）。

Rebuild 流程（07-storage-and-index.md §4）：

```text
新建下一版本号 DB 文件
→ replay raw.jsonl（按 seq）
→ replay evolution.jsonl（按 seq）
→ 构造所有表
→ integrity checks + WAL checkpoint
→ 切换指针（版本化文件 + current-db.txt，Windows 兼容）
→ 延迟清理旧版本文件
```

重放读取端排序唯一依据是 ``seq``（02-data-model.md §2）；``timestamp``
只作展示字段存入投影。重建全程持有 Ledger 写锁（与写入方同一串行化
策略），保证重放看到的是一致的账本快照。

投影失败只影响新版本文件：指针未切换前旧版本仍可读，Canonical Ledger
永不被修改或回滚（07 §5"绝不能为了 SQLite 回滚已经持久化的真实历史"）。
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.errors import PelError, StorageError
from project_evolution.index.schema import (
    PROJECTION_SCHEMA_VERSION,
    PROJECTION_TABLES,
    check_projection_integrity,
    create_schema,
    get_meta,
    set_meta,
)
from project_evolution.index.store import ProjectionStore
from project_evolution.ledger import EvolutionEventLedger, RawEventLedger
from project_evolution.ledger.lock import write_lock
from project_evolution.models import (
    BugFix,
    Decision,
    EvolutionEvent,
    FeatureChange,
    ParameterChange,
    RawEvent,
    check_task_transition,
)
from project_evolution.tasks.state import (
    # _LIFECYCLE_TARGET 是 Task 状态机的固定转换表，跨模块共用避免两份定义漂移
    _LIFECYCLE_TARGET,
    SessionRuntimeState,
    TaskRuntimeState,
    project_session_states,
    project_task_session_links,
    project_task_states,
)

#: raw event kind -> file change 动作（rename 记为 file_modified + payload.change）
_FILE_KINDS = {"file_added": "added", "file_modified": "modified", "file_deleted": "deleted"}

#: rebuild 持锁时间预算（线性扫描 10k~100k 事件量级）
_REBUILD_LOCK_TIMEOUT = 120.0


class ProjectionError(StorageError):
    """投影构建失败（账本语义不合法 / 数据库写入失败 / 自检失败）。"""


def normalize_name(name: str) -> str:
    """语义名归一化：小写 + 折叠空白（parameter/feature/decision 查询键）。

    纯函数、无状态，保证 rebuild 与 incremental 产出一致。
    """
    return " ".join(name.lower().split())


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ProjectionResult(BaseModel):
    """一次投影操作的产物统计。"""

    version: int
    db_path: str
    counts: dict[str, int] = Field(default_factory=dict)


class RebuildReport(ProjectionResult):
    """rebuild_projection 的结果。"""

    previous_version: int
    duration_ms: float
    removed_old_files: list[str] = Field(default_factory=list)


def _ledger_paths(root: Path, config: PelConfig) -> tuple[Path, Path, Path]:
    pel = root / PEL_DIR_NAME
    return (
        pel / config.events_dir / "raw.jsonl",
        pel / config.events_dir / "evolution.jsonl",
        pel / config.runtime_dir / "locks" / "ledger.lock",
    )


# --------------------------------------------------------------------- rows


def _raw_event_row(event: RawEvent) -> tuple:
    return (
        event.id,
        event.seq,
        event.project_id,
        event.timestamp.isoformat(),
        event.kind,
        event.source,
        event.task_id,
        event.session_id,
        event.checkpoint_id,
        _json(event.payload),
    )


def _file_change_row(event: RawEvent) -> tuple | None:
    base = _FILE_KINDS.get(event.kind)
    if base is None:
        return None
    payload = event.payload
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise ProjectionError(
            f"{event.kind} event {event.id} payload.path must be a non-empty string"
        )
    change = payload.get("change") if isinstance(payload.get("change"), str) else base
    old_path = payload.get("old_path")
    return (
        event.id,
        event.seq,
        path,
        old_path if isinstance(old_path, str) else None,
        change,
        payload.get("content_hash"),
        payload.get("evidence_id"),
        event.task_id,
        event.checkpoint_id,
        event.session_id,
        event.timestamp.isoformat(),
    )


def _checkpoint_row(event: RawEvent) -> tuple:
    assert event.checkpoint_id is not None
    payload = event.payload
    index = payload.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise ProjectionError(
            f"checkpoint_created event {event.id} payload.index must be an integer"
        )
    return (
        event.checkpoint_id,
        event.project_id,
        event.task_id,
        event.session_id,
        index,
        payload.get("reason"),
        payload.get("summary"),
        event.timestamp.isoformat(),
        payload.get("base_ref"),
        payload.get("head_ref"),
        payload.get("scope_confidence"),
        payload.get("diff_evidence_id"),
        payload.get("snapshot_path"),
    )


def _checkpoint_evidence_row(event: RawEvent) -> tuple | None:
    """checkpoint_created 中的 diff evidence -> evidence 行（无则 None）。"""
    payload = event.payload
    evidence_id = payload.get("diff_evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id:
        return None
    metadata = {
        "task_id": event.task_id,
        "checkpoint_id": event.checkpoint_id,
        "snapshot_path": payload.get("snapshot_path"),
        "base_ref": payload.get("base_ref"),
        "head_ref": payload.get("head_ref"),
    }
    return (
        evidence_id,
        event.project_id,
        "git_diff",
        payload.get("diff_evidence_sha256") or "",
        None,
        event.timestamp.isoformat(),
        _json(metadata),
        event.task_id,
        event.checkpoint_id,
    )


def _evolution_event_row(event: EvolutionEvent) -> tuple:
    return (
        event.id,
        event.seq,
        event.project_id,
        event.task_id,
        event.checkpoint_id,
        event.timestamp.isoformat(),
        event.type,
        event.title,
        event.description,
        _json(event.evidence_ids),
        event.confidence,
        event.analyzer_version,
        _json(event.payload),
    )


def _validated_payload(event: EvolutionEvent, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(event.payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        raise ProjectionError(
            f"evolution event {event.id} ({event.type}) has invalid payload "
            f"at .{first['loc']}: {first['msg']}"
        ) from exc


def _parameter_row(event: EvolutionEvent) -> tuple:
    payload = _validated_payload(event, ParameterChange)
    return (
        event.id,
        event.seq,
        normalize_name(payload.name),
        payload.name,
        payload.scope,
        payload.before,
        payload.after,
        payload.unit,
        payload.reason,
        event.task_id,
        event.checkpoint_id,
        event.timestamp.isoformat(),
    )


def _feature_row(event: EvolutionEvent) -> tuple:
    payload = _validated_payload(event, FeatureChange)
    return (
        event.id,
        event.seq,
        normalize_name(payload.feature),
        payload.feature,
        payload.action,
        payload.scope,
        payload.before,
        payload.after,
        event.task_id,
        event.checkpoint_id,
        event.timestamp.isoformat(),
    )


def _decision_row(event: EvolutionEvent) -> tuple:
    payload = _validated_payload(event, Decision)
    return (
        event.id,
        event.seq,
        normalize_name(payload.subject),
        payload.subject,
        payload.decision,
        payload.reason,
        _json(payload.supersedes),
        event.task_id,
        event.checkpoint_id,
        event.timestamp.isoformat(),
    )


def _bug_fix_row(event: EvolutionEvent) -> tuple:
    payload = _validated_payload(event, BugFix)
    return (
        event.id,
        event.seq,
        payload.problem,
        payload.cause,
        payload.fix,
        _json(payload.affected_scope),
        payload.verification,
        event.task_id,
        event.checkpoint_id,
        event.timestamp.isoformat(),
    )


def _task_row(state: TaskRuntimeState) -> tuple:
    return (
        state.task_id,
        state.project_id,
        state.title,
        state.status,
        state.started_at.isoformat(),
        state.finished_at.isoformat() if state.finished_at else None,
        None,  # milestone_id：V0.1 无 milestone 事件，列预留（07 §6）
        state.baseline_ref,
        state.final_ref,
    )


def _session_row(state: SessionRuntimeState) -> tuple:
    return (
        state.session_id,
        state.project_id,
        state.agent,
        state.started_at.isoformat(),
        state.ended_at.isoformat() if state.ended_at else None,
        state.external_session_id,
    )


def _link_row(link) -> tuple:
    return (
        link.task_id,
        link.session_id,
        link.joined_at.isoformat(),
        link.left_at.isoformat() if link.left_at else None,
    )


#: evolution type -> (INSERT SQL, 行构造器)；列数见 schema.py 各表定义
_SEMANTIC_INSERTS = {
    "parameter_change": (
        "INSERT INTO parameter_changes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        _parameter_row,
    ),
    "feature_change": (
        "INSERT INTO feature_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        _feature_row,
    ),
    "decision": (
        "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
        _decision_row,
    ),
    "bug_fix": (
        "INSERT INTO bug_fixes VALUES (?,?,?,?,?,?,?,?,?,?)",
        _bug_fix_row,
    ),
}


# ------------------------------------------------------------------ rebuild


def _insert_many(conn, sql: str, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(sql, rows)


def _replay_full(
    conn, raw_events: list[RawEvent], evolution_events: list[EvolutionEvent]
) -> None:
    """按 seq 顺序的全量重放（输入已由 ledger.iter_events 保证有序）。"""
    _insert_many(
        conn, "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        [_raw_event_row(event) for event in raw_events],
    )
    _insert_many(
        conn, "INSERT INTO file_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [row for row in map(_file_change_row, raw_events) if row is not None],
    )
    checkpoint_rows = [
        _checkpoint_row(event)
        for event in raw_events
        if event.kind == "checkpoint_created"
    ]
    _insert_many(
        conn, "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", checkpoint_rows
    )
    _insert_many(
        conn, "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?,?)",
        [
            row
            for row in map(_checkpoint_evidence_row, raw_events)
            if row is not None
        ],
    )

    # 领域状态复用 phase-03 投影函数：非法序列在这里抛 InvalidStateError，
    # rebuild 失败但 Canonical Ledger 不受影响
    task_states = project_task_states(raw_events)
    _insert_many(
        conn, "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        [_task_row(state) for state in task_states.values()],
    )
    _insert_many(
        conn, "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        [_session_row(state) for state in project_session_states(raw_events).values()],
    )
    _insert_many(
        conn, "INSERT INTO task_sessions VALUES (?,?,?,?)",
        [_link_row(link) for link in project_task_session_links(raw_events)],
    )

    _insert_many(
        conn, "INSERT INTO evolution_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_evolution_event_row(event) for event in evolution_events],
    )
    for type_name, (sql, row_builder) in _SEMANTIC_INSERTS.items():
        _insert_many(
            conn,
            sql,
            [
                row_builder(event)
                for event in evolution_events
                if event.type == type_name
            ],
        )


def _table_counts(conn) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in PROJECTION_TABLES
    }


def rebuild_projection(
    project_root: str | Path, *, config: PelConfig | None = None
) -> RebuildReport:
    """删除 DB 后可完整重建：按 seq 重放两条账本并切换指针（phase-06 §4）。

    :raises ProjectionError: 账本语义不合法、数据库写入失败或自检失败。
    """
    root = Path(project_root)
    config = config if config is not None else load_config(root)
    raw_path, evo_path, lock_path = _ledger_paths(root, config)
    store = ProjectionStore(root / PEL_DIR_NAME / config.runtime_dir)
    raw_ledger = RawEventLedger(root, events_path=raw_path, lock_path=lock_path)
    evo_ledger = EvolutionEventLedger(root, events_path=evo_path, lock_path=lock_path)

    started = time.perf_counter()
    previous_version = store.current_version()
    with write_lock(lock_path, timeout=_REBUILD_LOCK_TIMEOUT):
        raw_events = list(raw_ledger.iter_events())
        evolution_events = list(evo_ledger.iter_events())
        version = store.next_version()
        target = store.db_path(version)
        try:
            _build_database(store, target, raw_events, evolution_events)
        except PelError:
            _discard_partial(store, target)
            raise
        except BaseException as exc:  # noqa: BLE001 - 任何失败都不能留下可用假象
            _discard_partial(store, target)
            raise ProjectionError(f"rebuild failed: {exc}") from exc
        store.switch_pointer(version)
    removed = store.cleanup_old_versions(keep=version)

    conn = store.connect(target)
    try:
        counts = _table_counts(conn)
    finally:
        conn.close()
    return RebuildReport(
        version=version,
        db_path=str(target),
        counts=counts,
        previous_version=previous_version,
        duration_ms=(time.perf_counter() - started) * 1000,
        removed_old_files=[str(path) for path in removed],
    )


def _build_database(
    store: ProjectionStore,
    target: Path,
    raw_events: list[RawEvent],
    evolution_events: list[EvolutionEvent],
) -> None:
    """在全新文件上建 schema、重放、自检、checkpoint；失败由调用方清理。

    ``executescript`` 会隐式提交挂起事务，因此 DDL 在显式事务外执行
    （目标是全新文件，失败即整体丢弃，无需依赖事务回滚）。
    """
    conn = store.connect(target, create=True)
    try:
        create_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        _replay_full(conn, raw_events, evolution_events)
        set_meta(conn, "schema_version", str(PROJECTION_SCHEMA_VERSION))
        set_meta(conn, "raw_seq", str(raw_events[-1].seq if raw_events else 0))
        set_meta(conn, "evo_seq", str(evolution_events[-1].seq if evolution_events else 0))
        set_meta(conn, "stale", "0")
        set_meta(conn, "source", "rebuild")
        set_meta(conn, "rebuilt_at", datetime.now(timezone.utc).isoformat())
        conn.execute("COMMIT")
        issues = check_projection_integrity(conn)
        if issues:
            raise ProjectionError(
                f"projection integrity check failed: {'; '.join(issues[:5])}"
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _discard_partial(store: ProjectionStore, target: Path) -> None:
    """尽力删除未切换指针的半成品 DB（失败不影响正确性：指针未指向它）。"""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            target.with_name(target.name + suffix).unlink()


# -------------------------------------------------------------- incremental


class UpdateReport(ProjectionResult):
    """update_projection 的结果。"""

    status: Literal["updated", "up-to-date", "rebuilt"]
    applied_raw_events: int = 0
    applied_evolution_events: int = 0


def _payload_str(event: RawEvent, key: str) -> str:
    value = event.payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectionError(
            f"{event.kind} event {event.id} (seq={event.seq}) "
            f"payload.{key} must be a non-empty string"
        )
    return value


def _apply_task_started(conn, event: RawEvent) -> None:
    task_id = event.task_id
    if task_id is None:
        raise ProjectionError(f"task_started event {event.id} has no task_id")
    if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
        raise ProjectionError(f"duplicate task_started for task {task_id}")
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            event.project_id,
            _payload_str(event, "title"),
            "active",
            event.timestamp.isoformat(),
            None,
            None,
            event.payload.get("baseline_ref"),
            None,
        ),
    )
    if event.session_id:
        conn.execute(
            "INSERT OR IGNORE INTO task_sessions VALUES (?,?,?,?)",
            (task_id, event.session_id, event.timestamp.isoformat(), None),
        )


def _apply_task_lifecycle(conn, event: RawEvent) -> None:
    task_id = event.task_id
    if task_id is None:
        raise ProjectionError(f"{event.kind} event {event.id} has no task_id")
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ProjectionError(
            f"{event.kind} event {event.id} (seq={event.seq}) references unknown task {task_id}"
        )
    target = _LIFECYCLE_TARGET[event.kind]
    try:
        check_task_transition(str(row["status"]), target)
    except PelError as exc:
        raise ProjectionError(
            f"{event.kind} event {event.id} (seq={event.seq}): {exc}"
        ) from exc
    terminal = target in ("completed", "cancelled")
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, final_ref = ? WHERE task_id = ?",
        (
            target,
            event.timestamp.isoformat() if terminal else None,
            _opt_payload_str(event, "final_ref") if event.kind == "task_completed" else None,
            task_id,
        ),
    )
    if terminal:
        conn.execute(
            "UPDATE task_sessions SET left_at = ? "
            "WHERE task_id = ? AND left_at IS NULL",
            (event.timestamp.isoformat(), task_id),
        )


def _opt_payload_str(event: RawEvent, key: str) -> str | None:
    value = event.payload.get(key)
    if value is not None and not isinstance(value, str):
        raise ProjectionError(
            f"{event.kind} event {event.id} payload.{key} must be a string or null"
        )
    return value


def _apply_checkpoint_created(conn, event: RawEvent) -> None:
    assert event.checkpoint_id is not None
    task_id = event.task_id
    if task_id is None:
        raise ProjectionError(f"checkpoint_created event {event.id} has no task_id")
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ProjectionError(
            f"checkpoint_created event {event.id} references unknown task {task_id}"
        )
    if str(row["status"]) in ("completed", "cancelled"):
        raise ProjectionError(
            f"checkpoint_created event {event.id} (seq={event.seq}) on terminal "
            f"task {task_id} (status={row['status']})"
        )
    index = event.payload.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise ProjectionError(
            f"checkpoint_created event {event.id} payload.index must be an integer"
        )
    expected = int(
        conn.execute(
            "SELECT COALESCE(MAX(checkpoint_index), 0) + 1 FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    )
    if index != expected:
        raise ProjectionError(
            f"checkpoint index regression/out-of-order for task {task_id}: "
            f"expected {expected}, got {index} (event {event.id}, seq={event.seq})"
        )
    conn.execute(
        "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _checkpoint_row(event),
    )
    evidence = _checkpoint_evidence_row(event)
    if evidence is not None:
        conn.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?,?)", evidence)


def _apply_session_started(conn, event: RawEvent) -> None:
    session_id = event.session_id
    if session_id is None:
        raise ProjectionError(f"session_started event {event.id} has no session_id")
    if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone():
        raise ProjectionError(f"duplicate session_started for session {session_id}")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (
            session_id,
            event.project_id,
            _payload_str(event, "agent"),
            event.timestamp.isoformat(),
            None,
            _opt_payload_str(event, "external_session_id"),
        ),
    )


def _apply_session_ended(conn, event: RawEvent) -> None:
    session_id = event.session_id
    if session_id is None:
        raise ProjectionError(f"session_ended event {event.id} has no session_id")
    row = conn.execute(
        "SELECT ended_at FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ProjectionError(
            f"session_ended event {event.id} references unknown session {session_id}"
        )
    if row["ended_at"] is not None:
        raise ProjectionError(f"duplicate session_ended for session {session_id}")
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
        (event.timestamp.isoformat(), session_id),
    )
    conn.execute(
        "UPDATE task_sessions SET left_at = ? WHERE session_id = ? AND left_at IS NULL",
        (event.timestamp.isoformat(), session_id),
    )


def _apply_task_session_bound(conn, event: RawEvent) -> None:
    task_id = event.task_id
    session_id = event.session_id
    if task_id is None:
        raise ProjectionError(f"task_session_bound event {event.id} has no task_id")
    if session_id is None:
        raise ProjectionError(f"task_session_bound event {event.id} has no session_id")
    if not conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
        raise ProjectionError(
            f"task_session_bound event {event.id} references unknown task {task_id}"
        )
    # 同一 Session 转绑其他 Task 时关闭旧的 open link（与全量投影语义一致）
    conn.execute(
        "UPDATE task_sessions SET left_at = ? "
        "WHERE session_id = ? AND task_id != ? AND left_at IS NULL",
        (event.timestamp.isoformat(), session_id, task_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO task_sessions VALUES (?,?,?,?)",
        (task_id, session_id, event.timestamp.isoformat(), None),
    )


def _apply_file_event(conn, event: RawEvent) -> None:
    row = _file_change_row(event)
    if row is not None:
        conn.execute("INSERT INTO file_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)


_RAW_HANDLERS = {
    "task_started": _apply_task_started,
    "task_paused": _apply_task_lifecycle,
    "task_resumed": _apply_task_lifecycle,
    "task_completed": _apply_task_lifecycle,
    "task_cancelled": _apply_task_lifecycle,
    "checkpoint_created": _apply_checkpoint_created,
    "session_started": _apply_session_started,
    "session_ended": _apply_session_ended,
    "task_session_bound": _apply_task_session_bound,
    "file_added": _apply_file_event,
    "file_modified": _apply_file_event,
    "file_deleted": _apply_file_event,
}


def _apply_raw_event(conn, event: RawEvent) -> None:
    conn.execute(
        "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?)", _raw_event_row(event)
    )
    handler = _RAW_HANDLERS.get(event.kind)
    if handler is not None:
        handler(conn, event)


def _apply_evolution_event(conn, event: EvolutionEvent) -> None:
    conn.execute(
        "INSERT INTO evolution_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _evolution_event_row(event),
    )
    sql, row_builder = _SEMANTIC_INSERTS[event.type]
    conn.execute(sql, row_builder(event))


def _mark_stale(store: ProjectionStore) -> None:
    """SQLite 更新失败：标记 projection stale（Canonical Event 保持成功，07 §5）。"""
    with contextlib.suppress(Exception):
        conn = store.connect(store.db_path(store.current_version()))
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            set_meta(conn, "stale", "1")
        finally:
            conn.close()


def update_projection(
    project_root: str | Path, *, config: PelConfig | None = None
) -> UpdateReport:
    """把当前 DB 增量追平到 Ledger head（07-storage-and-index.md §5）。

    - 无 DB（首次/被删）或 schema 版本不兼容 -> 自动 full rebuild；
    - 持有与写入方相同的 Ledger 写锁，读水位 -> 应用新事件 -> 推进水位，
      整体在一个 SQLite 事务内；
    - SQLite 更新失败：Canonical Ledger 不回滚，projection 标记 stale，
      后续 rebuild 即可恢复（07 §5"绝不能为了 SQLite 回滚真实历史"）。

    :raises ProjectionError: 应用失败（水位未推进，DB 标记 stale）。
    """
    root = Path(project_root)
    config = config if config is not None else load_config(root)
    raw_path, evo_path, lock_path = _ledger_paths(root, config)
    store = ProjectionStore(root / PEL_DIR_NAME / config.runtime_dir)
    raw_ledger = RawEventLedger(root, events_path=raw_path, lock_path=lock_path)
    evo_ledger = EvolutionEventLedger(root, events_path=evo_path, lock_path=lock_path)

    version = store.current_version()
    if (
        version < 1
        or not store.db_path(version).exists()
        or _projection_schema_mismatch(store, version)
    ):
        report = rebuild_projection(root, config=config)
        return UpdateReport(
            status="rebuilt",
            version=report.version,
            db_path=report.db_path,
            counts=report.counts,
        )

    with write_lock(lock_path, timeout=_REBUILD_LOCK_TIMEOUT):
        conn = store.connection()
        raw_watermark = int(get_meta(conn, "raw_seq") or "0")
        evo_watermark = int(get_meta(conn, "evo_seq") or "0")
        new_raw = [
            event for event in raw_ledger.iter_events() if (event.seq or 0) > raw_watermark
        ]
        new_evo = [
            event for event in evo_ledger.iter_events() if (event.seq or 0) > evo_watermark
        ]
        try:
            conn.execute("BEGIN IMMEDIATE")
            for event in new_raw:
                _apply_raw_event(conn, event)
            for evolution_event in new_evo:
                _apply_evolution_event(conn, evolution_event)
            if new_raw:
                set_meta(conn, "raw_seq", str(new_raw[-1].seq))
            if new_evo:
                set_meta(conn, "evo_seq", str(new_evo[-1].seq))
            set_meta(conn, "stale", "0")  # 追平即清除 stale（事务保证内容一致）
            set_meta(conn, "source", "incremental")
            set_meta(conn, "updated_at", datetime.now(timezone.utc).isoformat())
            conn.execute("COMMIT")
            counts = _table_counts(conn)
        except PelError:
            _rollback(conn)
            _mark_stale(store)
            raise
        except BaseException as exc:  # noqa: BLE001 - 失败必须标记 stale 并显式抛出
            _rollback(conn)
            _mark_stale(store)
            raise ProjectionError(f"incremental projection failed: {exc}") from exc
        finally:
            # 写路径连接短生命周期：显式释放，Windows 下不残留文件句柄
            store.close()

    status: Literal["updated", "up-to-date"] = (
        "updated" if (new_raw or new_evo) else "up-to-date"
    )
    return UpdateReport(
        status=status,
        version=version,
        db_path=str(store.db_path(version)),
        applied_raw_events=len(new_raw),
        applied_evolution_events=len(new_evo),
        counts=counts,
    )


def _projection_schema_mismatch(store: ProjectionStore, version: int) -> bool:
    """当前 DB 的投影 schema 版本与本实现不兼容（未来版本的显式迁移点）。"""
    conn = store.connect(store.db_path(version))
    try:
        current = get_meta(conn, "schema_version")
    finally:
        conn.close()
    return current is not None and int(current) != PROJECTION_SCHEMA_VERSION


def _rollback(conn) -> None:
    if conn.in_transaction:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
