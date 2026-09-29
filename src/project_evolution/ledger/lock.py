"""项目级写锁。

同一项目同一时刻只允许一个 Ledger writer（docs/01-architecture/01-system-architecture.md §8）。
锁文件位于 ``.project-evolution/.runtime/locks/ledger.lock``，
通过操作系统文件锁（Windows: msvcrt / POSIX: fcntl）跨进程、跨线程互斥。
读操作不需要独占锁。
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from project_evolution.errors import StorageError

_RETRY_INTERVAL = 0.005


class LockTimeoutError(StorageError):
    """在超时时间内未能获得项目写锁。"""


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # type: ignore[import-not-found]

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # type: ignore[import-not-found]

        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]


@contextlib.contextmanager
def write_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """获取项目级独占写锁。

    非重入：持有期间再次获取会等到超时。调用方不得嵌套加锁。

    :raises LockTimeoutError: 超时未获得锁。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 句柄必须跨 yield 存活，因此不能改成 with 语句
    fh = open(path, "a+b")  # noqa: SIM115
    fd = fh.fileno()
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"could not acquire ledger write lock: {path}") from None
                time.sleep(_RETRY_INTERVAL)
        yield
    finally:
        if acquired:
            _unlock(fd)
        fh.close()
