"""事件向量索引：SQLite 表存归一化向量，暴力点积做余弦召回。

定位（与 07-storage-and-index 一致）：这是**可丢弃的派生层**——
重建投影（换版本 db 文件）后向量表随旧文件消失，下次搜索按需重嵌即可；
ledger JSONL 永远是真相源。向量只回答"哪些事件相关"（返回 ID 引用），
不参与事实存储。
"""

from __future__ import annotations

import array
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from project_evolution.retrieval.embedder import Embedder

_TABLE = "event_vectors"


@dataclass(frozen=True)
class VectorHit:
    event_id: str
    score: float


def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            event_id   TEXT PRIMARY KEY,
            model_id   TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vec        BLOB NOT NULL,
            embedded_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def ensure_vectors(
    con: sqlite3.Connection,
    rows: Sequence[tuple[str, str]],
    embedder: Embedder,
) -> int:
    """为缺失向量的事件（(event_id, text) 列表）补嵌；换模型自动清旧向量。

    返回本次实际嵌入的条数（0 = 全部命中缓存）。
    """
    _ensure_table(con)
    existing = {
        str(row["event_id"])
        for row in con.execute(
            f"SELECT event_id FROM {_TABLE} WHERE model_id = ?", (embedder.name,)
        )
    }
    stale = [
        str(row["event_id"])
        for row in con.execute(
            f"SELECT event_id FROM {_TABLE} WHERE model_id != ?", (embedder.name,)
        )
    ]
    if stale:
        con.executemany(
            f"DELETE FROM {_TABLE} WHERE event_id = ?", [(eid,) for eid in stale]
        )
        con.commit()

    pending = [(eid, text) for eid, text in rows if eid not in existing]
    if not pending:
        return 0

    vectors = embedder.embed([text for _eid, text in pending])
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        f"INSERT OR REPLACE INTO {_TABLE} (event_id, model_id, dim, vec, embedded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                eid,
                embedder.name,
                len(vec),
                array.array("f", vec).tobytes(),
                now,
            )
            for (eid, _text), vec in zip(pending, vectors, strict=True)
        ],
    )
    con.commit()
    return len(pending)


def search_vectors(
    con: sqlite3.Connection,
    query: str,
    embedder: Embedder,
    *,
    limit: int = 10,
) -> list[VectorHit]:
    """全量点积（向量已归一化，点积即余弦），返回 top-k 事件 ID 引用。"""
    _ensure_table(con)
    (query_vec,) = embedder.embed_query([query]) if query.strip() else ([],)
    if not query_vec:
        return []

    hits: list[VectorHit] = []
    for row in con.execute(
        f"SELECT event_id, vec FROM {_TABLE} WHERE model_id = ?", (embedder.name,)
    ):
        stored = array.array("f")
        stored.frombytes(row["vec"])
        score = sum(a * b for a, b in zip(query_vec, stored, strict=False))
        hits.append(VectorHit(event_id=str(row["event_id"]), score=score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
