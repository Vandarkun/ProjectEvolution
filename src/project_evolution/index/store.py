"""Projection 运行时存储：版本化 DB 文件 + 指针切换（07-storage-and-index.md §4）。

Windows 不允许替换被其他进程打开的文件，因此禁止"长连接读旧库 + 原子替换单文件"。
本模块实现文档规定的版本化协议：

```text
.project-evolution/.runtime/
├── evolution-<n>.db    实际数据库，n 单调递增
└── current-db.txt      指针文件，内容为当前 n
```

- 重建写入 ``evolution-<n+1>.db``，完成后原子更新指针
  （小文件 tmp + ``os.replace``，带重试）；
- 取连接前读指针；连接按版本缓存，指针变化后重建连接并释放旧句柄；
- 旧版本文件延迟清理，删除失败不影响正确性；
- 所有连接使用 WAL 模式 + ``busy_timeout``。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from project_evolution.errors import StorageError

_LOG = logging.getLogger(__name__)

POINTER_FILENAME = "current-db.txt"
DB_BASENAME = "evolution"
DB_VERSION_RE = re.compile(r"^evolution-(\d+)\.db$")
#: os.replace 指针切换的重试参数（Windows 上杀毒软件等可能短暂占用目标文件）
_SWITCH_RETRIES = 8
_SWITCH_RETRY_DELAY = 0.01
#: SQLite busy_timeout（毫秒）；与 07 §4 "所有连接使用 WAL + busy_timeout" 对齐
BUSY_TIMEOUT_MS = 5000


class ProjectionNotBuiltError(StorageError):
    """指针不存在（或指向的数据库缺失）：需要先执行 rebuild。"""


class ProjectionStore:
    """单个项目 runtime 目录下的版本化数据库与指针管理。"""

    def __init__(self, runtime_dir: str | Path):
        self._dir = Path(runtime_dir)
        self._lock = threading.Lock()
        self._cached_version: int | None = None
        self._cached_conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- layout

    @property
    def runtime_dir(self) -> Path:
        return self._dir

    @property
    def pointer_path(self) -> Path:
        return self._dir / POINTER_FILENAME

    def db_path(self, version: int) -> Path:
        if version < 1:
            raise StorageError(f"db version must be >= 1, got {version}")
        return self._dir / f"{DB_BASENAME}-{version}.db"

    # --------------------------------------------------------------- pointer

    def current_version(self) -> int:
        """读指针得到当前版本；指针缺失返回 0（等价于"尚未建库"）。

        指针内容非法（非整数）不抛异常：按 0 处理，查询侧会得到明确的
        "需要 rebuild" 错误，重建即可恢复——不留下死路。
        """
        try:
            text = self.pointer_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            _LOG.warning("cannot read projection pointer %s: %s", self.pointer_path, exc)
            return 0
        try:
            return int(text)
        except ValueError:
            _LOG.warning(
                "corrupt projection pointer %s: %r; treating as not built",
                self.pointer_path,
                text[:64],
            )
            return 0

    def switch_pointer(self, version: int) -> None:
        """原子切换指针到 ``version``（tmp 写入 + ``os.replace``，带重试）。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer_path.with_name(POINTER_FILENAME + ".tmp")
        tmp.write_text(f"{version}\n", encoding="ascii")
        last_error: OSError | None = None
        for attempt in range(_SWITCH_RETRIES):
            try:
                os.replace(tmp, self.pointer_path)
                return
            except OSError as exc:  # Windows: 目标被短暂占用（AV 扫描等）
                last_error = exc
                time.sleep(_SWITCH_RETRY_DELAY * (attempt + 1))
        raise StorageError(
            f"cannot switch projection pointer to version {version} "
            f"after {_SWITCH_RETRIES} attempts: {self.pointer_path}: {last_error}"
        ) from last_error

    # ------------------------------------------------------------- versions

    def existing_versions(self) -> list[int]:
        """runtime 目录中实际存在的 DB 版本号（升序）。"""
        if not self._dir.is_dir():
            return []
        versions: list[int] = []
        for entry in self._dir.iterdir():
            match = DB_VERSION_RE.match(entry.name)
            if entry.is_file() and match:
                versions.append(int(match.group(1)))
        return sorted(versions)

    def next_version(self) -> int:
        """下一个可写版本号：max(当前指针, 已存在文件) + 1（n 单调递增）。"""
        return max([self.current_version(), *self.existing_versions()], default=0) + 1

    def cleanup_old_versions(self, keep: int) -> list[Path]:
        """删除版本号小于 ``keep`` 的旧 DB 及其 WAL/SHM 边车文件（尽力而为）。

        Windows 上旧连接可能仍持有文件句柄导致删除失败——忽略并留给下一次
        重建重试；失败绝不影响正确性（指针已指向新版本）。
        """
        removed: list[Path] = []
        for version in self.existing_versions():
            if version >= keep:
                continue
            for path in (
                self.db_path(version),
                self.db_path(version).with_name(self.db_path(version).name + "-wal"),
                self.db_path(version).with_name(self.db_path(version).name + "-shm"),
            ):
                try:
                    path.unlink()
                    removed.append(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _LOG.debug("delayed cleanup skipped %s: %s", path, exc)
        return removed

    # ------------------------------------------------------------ connection

    @staticmethod
    def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
        """打开一个 DB 连接：WAL 模式 + busy_timeout + Row 行工厂。

        ``create=False`` 时要求文件已存在（查询路径绝不凭空建库）。
        """
        if not create and not path.exists():
            raise ProjectionNotBuiltError(
                f"projection database not found: {path} (run `pel index rebuild`)"
            )
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connection(self) -> sqlite3.Connection:
        """取当前版本的连接（按版本缓存；指针变化后重建并释放旧句柄）。

        缓存连接若已被外部关闭，自动重开——调用方可以按需 close 取得的
        连接而不破坏池。

        :raises ProjectionNotBuiltError: 指针不存在，或指针指向的文件缺失。
        """
        version = self.current_version()
        if version < 1:
            raise ProjectionNotBuiltError(
                f"projection not built yet (no pointer): {self.pointer_path}; "
                "run `pel index rebuild`"
            )
        with self._lock:
            if self._cached_version == version and self._cached_conn is not None:
                try:
                    self._cached_conn.execute("SELECT 1")  # 存活检查
                    return self._cached_conn
                except sqlite3.Error:
                    _close_quietly(self._cached_conn)
                    self._cached_conn = None
            conn = self.connect(self.db_path(version))  # 文件缺失 -> 明确报错
            old = self._cached_conn
            self._cached_version = version
            self._cached_conn = conn
        _close_quietly(old)
        return conn

    def close(self) -> None:
        """释放缓存连接（下次 connection() 重新打开）。"""
        with self._lock:
            old = self._cached_conn
            self._cached_conn = None
            self._cached_version = None
        _close_quietly(old)

    def __enter__(self) -> ProjectionStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error as exc:  # pragma: no cover - close 极少失败
        _LOG.debug("closing stale projection connection failed: %s", exc)
