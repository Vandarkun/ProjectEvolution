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
from typing import Any

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.errors import PelError, StorageError
from project_evolution.index.schema import (
    PROJECTION_SCHEMA_VERSION,
    PROJECTION_TABLES,
    check_projection_integrity,
    create_schema,
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
)
from project_evolution.tasks.state import (
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
