"""SQLite Projection 的 Schema 与元数据访问（phase-06 §5）。

表集合按 07-storage-and-index.md §6 的 V0.1 语义设计：

- ``raw_events`` / ``evolution_events``：两条账本的逐行投影（payload 存 JSON），
  语义表（parameter/feature/decision/bug）从 evolution 行进一步展开；
- ``tasks`` / ``sessions`` / ``task_sessions`` / ``checkpoints``：复用 phase-03
  投影函数的领域状态（rebuild 用全量重放，incremental 用逐事件状态迁移）；
- ``evidence``：从 Ledger 可重建的 Evidence 记录（checkpoint diff evidence 等）；
- ``file_changes``：file_added/modified/deleted RawEvent 的展开（get_file_history
  的索引基础，rename 保留 old_path 以便沿链追踪）；
- ``projection_meta``：SQLite 与 Ledger head 的同步标记
  （raw_seq / evo_seq 水位、stale 标记、schema 版本）。

SQLite 只是投影（07 §3）：不保存唯一 Event / Evidence，删除后永远可由
Ledger 重放重建。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 投影 schema 版本（与事件 schema_version 独立；不兼容变更时递增并显式迁移）
PROJECTION_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE projection_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    milestone_id TEXT,
    baseline_ref TEXT,
    final_ref    TEXT
);

CREATE TABLE sessions (
    session_id          TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    agent               TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    external_session_id TEXT
);

CREATE TABLE task_sessions (
    task_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    joined_at  TEXT NOT NULL,
    left_at    TEXT,
    PRIMARY KEY (task_id, session_id)
);

CREATE TABLE checkpoints (
    checkpoint_id     TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    session_id        TEXT,
    checkpoint_index  INTEGER NOT NULL,
    reason            TEXT,
    summary           TEXT,
    created_at        TEXT NOT NULL,
    base_ref          TEXT,
    head_ref          TEXT,
    scope_confidence  TEXT,
    diff_evidence_id  TEXT,
    snapshot_path     TEXT
);
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, checkpoint_index);

CREATE TABLE raw_events (
    event_id      TEXT PRIMARY KEY,
    seq           INTEGER NOT NULL UNIQUE,
    project_id    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,
    task_id       TEXT,
    session_id    TEXT,
    checkpoint_id TEXT,
    payload       TEXT NOT NULL
);
CREATE INDEX idx_raw_task ON raw_events(task_id, seq);
CREATE INDEX idx_raw_checkpoint ON raw_events(checkpoint_id);
CREATE INDEX idx_raw_kind ON raw_events(kind, seq);

CREATE TABLE evolution_events (
    event_id         TEXT PRIMARY KEY,
    seq              INTEGER NOT NULL UNIQUE,
    project_id       TEXT NOT NULL,
    task_id          TEXT,
    checkpoint_id    TEXT,
    timestamp        TEXT NOT NULL,
    type             TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL,
    evidence_ids     TEXT NOT NULL,
    confidence       REAL,
    analyzer_version TEXT NOT NULL,
    payload          TEXT NOT NULL
);
CREATE INDEX idx_evo_task ON evolution_events(task_id, seq);
CREATE INDEX idx_evo_type ON evolution_events(type, seq);

CREATE TABLE evidence (
    evidence_id   TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    type          TEXT NOT NULL,
    ref           TEXT NOT NULL,
    path          TEXT,
    created_at    TEXT NOT NULL,
    metadata      TEXT NOT NULL,
    task_id       TEXT,
    checkpoint_id TEXT
);

CREATE TABLE parameter_changes (
    event_id     TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL,
    name_norm    TEXT NOT NULL,
    name_display TEXT NOT NULL,
    scope        TEXT,
    before_value TEXT,
    after_value  TEXT,
    unit         TEXT,
    reason       TEXT,
    task_id      TEXT,
    checkpoint_id TEXT,
    timestamp    TEXT NOT NULL
);
CREATE INDEX idx_param_name ON parameter_changes(name_norm, seq);

CREATE TABLE feature_changes (
    event_id          TEXT PRIMARY KEY,
    seq               INTEGER NOT NULL,
    feature_name_norm TEXT NOT NULL,
    feature_display   TEXT NOT NULL,
    action            TEXT NOT NULL,
    scope             TEXT,
    before_desc       TEXT,
    after_desc        TEXT,
    task_id           TEXT,
    checkpoint_id     TEXT,
    timestamp         TEXT NOT NULL
);
CREATE INDEX idx_feature_name ON feature_changes(feature_name_norm, seq);

CREATE TABLE decisions (
    event_id       TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL,
    subject_norm   TEXT NOT NULL,
    subject_display TEXT NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT,
    supersedes     TEXT NOT NULL,
    task_id        TEXT,
    checkpoint_id  TEXT,
    timestamp      TEXT NOT NULL
);
CREATE INDEX idx_decision_subject ON decisions(subject_norm, seq);

CREATE TABLE bug_fixes (
    event_id       TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL,
    problem        TEXT NOT NULL,
    cause          TEXT,
    fix            TEXT NOT NULL,
    affected_scope TEXT NOT NULL,
    verification   TEXT,
    task_id        TEXT,
    checkpoint_id  TEXT,
    timestamp      TEXT NOT NULL
);
CREATE INDEX idx_bug_task ON bug_fixes(task_id, seq);

CREATE TABLE file_changes (
    event_id      TEXT PRIMARY KEY,
    seq           INTEGER NOT NULL,
    path          TEXT NOT NULL,
    old_path      TEXT,
    change        TEXT NOT NULL,
    content_hash  TEXT,
    evidence_id   TEXT,
    task_id       TEXT,
    checkpoint_id TEXT,
    session_id    TEXT,
    timestamp     TEXT NOT NULL
);
CREATE INDEX idx_file_path ON file_changes(path, seq);
CREATE INDEX idx_file_old_path ON file_changes(old_path, seq);
CREATE INDEX idx_file_checkpoint ON file_changes(checkpoint_id);
"""

#: 全部投影表（不含 projection_meta；一致性比较与行数统计使用）
PROJECTION_TABLES = (
    "tasks",
    "sessions",
    "task_sessions",
    "checkpoints",
    "raw_events",
    "evolution_events",
    "evidence",
    "parameter_changes",
    "feature_changes",
    "decisions",
    "bug_fixes",
    "file_changes",
)


def create_schema(conn: sqlite3.Connection) -> None:
    """在空数据库上创建全部表与索引（幂等：IF NOT EXISTS 语义由调用方保证全新文件）。"""
    conn.executescript(_DDL)


def check_projection_integrity(conn: sqlite3.Connection) -> list[str]:
    """运行 SQLite 自检；返回问题列表（空列表 = 通过）。

    07-storage-and-index.md §4：重建完成时执行 integrity checks + WAL checkpoint，
    自检失败的数据库绝不切换指针。
    """
    issues: list[str] = []
    for row in conn.execute("PRAGMA integrity_check"):
        message = row[0]
        if message != "ok":
            issues.append(str(message))
    for row in conn.execute("PRAGMA foreign_key_check"):
        issues.append(f"foreign_key_check: {tuple(row)}")
    return issues


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM projection_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO projection_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def schema_version(conn: sqlite3.Connection) -> int:
    """读 projection_meta 中的 schema 版本；空库返回 0（视为未建 schema）。"""
    try:
        value = get_meta(conn, "schema_version")
    except sqlite3.OperationalError:  # projection_meta 表不存在
        return 0
    return int(value) if value is not None else 0


def schema_version_of_file(path: Path) -> int:
    """打开（不创建）一个 DB 文件读 schema 版本；文件不存在返回 0。"""
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return schema_version(conn)
    finally:
        conn.close()
