"""ProjectionStore 测试：版本化 DB + 指针切换协议（07-storage-and-index.md §4）。

重点验证 Windows 兼容性：

- 指针切换 = tmp + ``os.replace``（无共享冲突死路）；
- 重建期间旧连接继续可读（旧版本文件不被替换、不被删除阻塞）；
- 指针变化后 connection() 重建连接并释放旧句柄；
- 旧版本文件延迟清理，失败（文件仍被打开）不影响正确性；
- 指针缺失/损坏 -> 明确的 "not built" 错误，rebuild 即可恢复。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from project_evolution.index.schema import (
    PROJECTION_SCHEMA_VERSION,
    check_projection_integrity,
    create_schema,
    get_meta,
    schema_version,
    schema_version_of_file,
    set_meta,
)
from project_evolution.index.store import (
    BUSY_TIMEOUT_MS,
    ProjectionNotBuiltError,
    ProjectionStore,
)


def make_store(tmp_path: Path) -> ProjectionStore:
    return ProjectionStore(tmp_path / ".project-evolution" / ".runtime")


def test_pointer_missing_means_version_zero(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.current_version() == 0
    assert store.existing_versions() == []
    with pytest.raises(ProjectionNotBuiltError):
        store.connection()


def test_corrupt_pointer_is_not_a_dead_end(tmp_path: Path) -> None:
    """指针内容损坏按未建库处理：明确报错，rebuild 可恢复。"""
    store = make_store(tmp_path)
    store.runtime_dir.mkdir(parents=True)
    store.pointer_path.write_text("not-a-number", encoding="ascii")
    assert store.current_version() == 0
    with pytest.raises(ProjectionNotBuiltError):
        store.connection()


def test_switch_pointer_is_atomic_and_readable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.switch_pointer(3)
    assert store.current_version() == 3
    # 指针是纯文本小文件
    assert store.pointer_path.read_text(encoding="ascii").strip() == "3"
    # tmp 文件不残留
    assert not (store.runtime_dir / "current-db.txt.tmp").exists()


def test_next_version_is_monotonic_over_files_and_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.next_version() == 1
    store.db_path(1).parent.mkdir(parents=True, exist_ok=True)
    store.db_path(1).write_bytes(b"")  # 上次重建失败留下的半成品
    assert store.next_version() == 2  # 不覆盖既有文件
    store.switch_pointer(5)
    assert store.next_version() == 6


def test_connection_wal_and_busy_timeout(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    conn = store.connect(store.db_path(1), create=True)
    create_schema(conn)
    set_meta(conn, "k", "v")
    assert get_meta(conn, "k") == "v"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    conn.close()
    assert schema_version_of_file(store.db_path(1)) == 0  # 未写 meta 的空 schema

    conn2 = store.connect(store.db_path(1))
    assert schema_version(conn2) == 0
    set_meta(conn2, "schema_version", str(PROJECTION_SCHEMA_VERSION))
    conn2.close()
    assert schema_version_of_file(store.db_path(1)) == PROJECTION_SCHEMA_VERSION


def test_integrity_check_detects_corruption(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    conn = store.connect(store.db_path(1), create=True)
    create_schema(conn)
    assert check_projection_integrity(conn) == []
    # 直接写坏 DB 文件字节（绕过 SQLite）后自检应报告问题
    conn.close()
    path = store.db_path(1)
    data = bytearray(path.read_bytes())
    garbage = b"\x00garbage-not-sqlite\x00"
    data[len(data) // 2 : len(data) // 2 + len(garbage)] = garbage
    path.write_bytes(bytes(data))
    broken = sqlite3.connect(path)
    try:
        assert check_projection_integrity(broken) != []
    finally:
        broken.close()


def test_connection_cache_follows_pointer_and_releases_old_handle(
    tmp_path: Path,
) -> None:
    """指针切换后 connection() 返回新库连接；旧句柄被释放（Windows 无死锁路径）。"""
    store = make_store(tmp_path)
    for version, marker in ((1, "one"), (2, "two")):
        conn = store.connect(store.db_path(version), create=True)
        create_schema(conn)
        set_meta(conn, "marker", marker)
        conn.close()
    store.switch_pointer(1)
    conn1 = store.connection()
    assert get_meta(conn1, "marker") == "one"

    store.switch_pointer(2)
    conn2 = store.connection()
    assert get_meta(conn2, "marker") == "two"
    # 旧连接在 Python 侧已被关闭；再次使用应报 ProgrammingError
    with pytest.raises(sqlite3.ProgrammingError):
        conn1.execute("SELECT 1")
    store.close()


def test_old_reader_survives_rebuild_style_switch(tmp_path: Path) -> None:
    """模拟重建切换：旧连接一直打开时，新版本照常写入、指针照常切换。

    这是 Windows sharing violation 的核心场景：本协议不替换旧文件，
    旧连接继续读旧版本，互不阻塞。
    """
    store = make_store(tmp_path)
    old_conn = store.connect(store.db_path(1), create=True)
    create_schema(old_conn)
    set_meta(old_conn, "generation", "1")  # isolation_level=None：即时生效

    new_conn = store.connect(store.db_path(2), create=True)
    create_schema(new_conn)
    set_meta(new_conn, "generation", "2")
    new_conn.close()
    store.switch_pointer(2)

    # 旧连接仍然可读、数据未受影响
    assert get_meta(old_conn, "generation") == "1"
    # 新读取者立即看到新版本
    assert get_meta(store.connection(), "generation") == "2"

    # 延迟清理旧文件：因 old_conn 仍持有句柄而失败（Windows）或成功（POSIX），
    # 两种结果都必须不影响指针与当前读取
    store.cleanup_old_versions(keep=2)
    assert store.current_version() == 2
    assert get_meta(store.connection(), "generation") == "2"
    old_conn.close()
    store.close()


def test_cleanup_removes_old_files_and_sidecars(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.runtime_dir.mkdir(parents=True)
    for version in (1, 2, 3):
        db = store.db_path(version)
        db.write_bytes(b"")
        db.with_name(db.name + "-wal").write_bytes(b"")
    store.switch_pointer(3)
    removed = store.cleanup_old_versions(keep=3)
    removed_names = {path.name for path in removed}
    assert "evolution-1.db" in removed_names
    assert "evolution-1.db-wal" in removed_names
    assert "evolution-2.db" in removed_names
    assert store.db_path(3).exists()
    assert store.existing_versions() == [3]


def test_pointer_to_missing_db_file_errors_clearly(tmp_path: Path) -> None:
    """指针存在但 DB 文件被手工删除：明确报错而不是凭空建空库。"""
    store = make_store(tmp_path)
    store.runtime_dir.mkdir(parents=True)
    store.switch_pointer(7)
    with pytest.raises(ProjectionNotBuiltError, match="rebuild"):
        store.connection()
