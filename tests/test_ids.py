"""ID 生成器测试：前缀、唯一性、格式、并发。"""

from __future__ import annotations

import re
import threading

import pytest

from project_evolution.errors import ValidationError
from project_evolution.ids import PREFIXES, new_id

# Crockford Base32 字符集（无 I L O U）
_ULID_RE = re.compile(r"^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")


@pytest.mark.parametrize("prefix", sorted(PREFIXES))
def test_prefixes(prefix: str) -> None:
    value = new_id(prefix)
    assert value.startswith(f"{prefix}-")
    assert _ULID_RE.match(value[len(prefix) + 1 :])


def test_unknown_prefix_rejected() -> None:
    with pytest.raises(ValidationError):
        new_id("BOGUS")
    with pytest.raises(ValidationError):
        new_id("")


def test_unique_within_process() -> None:
    ids = {new_id("TASK") for _ in range(2000)}
    assert len(ids) == 2000


def test_unique_across_prefixes() -> None:
    ids = {new_id("RAW") for _ in range(200)} | {new_id("EVO") for _ in range(200)}
    assert len(ids) == 400


def test_monotonic_non_decreasing_within_process() -> None:
    # ULID 时间戳在前：同进程生成的 ID 字典序不减小
    values = [new_id("EVD").split("-", 1)[1] for _ in range(1000)]
    assert values == sorted(values)


def test_unique_under_threads() -> None:
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [new_id("CHK") for _ in range(500)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2000
    assert len(set(results)) == 2000
