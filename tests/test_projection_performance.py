"""10k+ 事件规模性能测试（phase-06 §10，07-storage-and-index.md §12）。

目标量级（V0.1 不是海量平台，但必须"可接受"）：

- 10k+ raw 事件 + 1k+ evolution 事件的 rebuild 为线性扫描，秒级完成；
- 常规 Task 查询 / Parameter History 目标 < 200ms（测试用 500ms 硬门槛，
  留 2.5 倍余量给慢速环境）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_evolution.index import QueryService, rebuild_projection
from project_evolution.ledger import EvolutionEventLedger, RawEventLedger
from project_evolution.models import EvolutionEvent, RawEvent

PRJ = "PRJ-perf"
TASKS = 120
CHECKPOINTS_PER_TASK = 20
FILES_PER_CHECKPOINT = 5

#: 常规查询硬门槛（目标 200ms 的 2.5 倍，避免慢速机器误报）
QUERY_BUDGET_S = 0.5
#: rebuild 硬门槛（预期秒级；线性扫描 ~2 万事件）
REBUILD_BUDGET_S = 60.0


def generate_ledgers(project: Path) -> tuple[int, int]:
    """生成 ~2.3 万 raw 事件 + ~1.4 千 evolution 事件的确定性账本。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tick = 0

    def ts() -> datetime:
        nonlocal tick
        tick += 1
        return base + timedelta(seconds=tick)

    raw = RawEventLedger(project)
    evo = EvolutionEventLedger(project)
    raw_events: list[RawEvent] = []
    evo_events: list[EvolutionEvent] = []
    counter = 0

    for t in range(TASKS):
        task_id = f"TASK-T{t:04d}"
        counter += 1
        raw_events.append(
            RawEvent(
                id=f"RAW-{counter:022d}",
                project_id=PRJ,
                timestamp=ts(),
                kind="task_started",
                source="perf",
                task_id=task_id,
                payload={"title": f"任务 {t}"},
            )
        )
        for cp in range(1, CHECKPOINTS_PER_TASK + 1):
            checkpoint_id = f"CHK-T{t:04d}C{cp:03d}"
            counter += 1
            raw_events.append(
                RawEvent(
                    id=f"RAW-{counter:022d}",
                    project_id=PRJ,
                    timestamp=ts(),
                    kind="checkpoint_created",
                    source="perf",
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    payload={"index": cp, "summary": f"cp {cp}",
                             "diff_evidence_id": f"EVD-{counter:021d}",
                             "diff_evidence_sha256": "x" * 64},
                )
            )
            for f in range(FILES_PER_CHECKPOINT):
                counter += 1
                # 每个任务一个专属文件 + 所有任务共用的热点文件
                path = (
                    f"Core/File{t:04d}_{cp:03d}.cs"
                    if f < FILES_PER_CHECKPOINT - 1
                    else "Core/HotFile.cs"
                )
                raw_events.append(
                    RawEvent(
                        id=f"RAW-{counter:022d}",
                        project_id=PRJ,
                        timestamp=ts(),
                        kind="file_modified",
                        source="perf",
                        task_id=task_id,
                        checkpoint_id=checkpoint_id,
                        payload={"path": path, "content_hash": "h"},
                    )
                )
            counter += 1
            raw_events.append(
                RawEvent(
                    id=f"RAW-{counter:022d}",
                    project_id=PRJ,
                    timestamp=ts(),
                    kind="test_passed",
                    source="perf",
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    payload={"command": "pytest -q"},
                )
            )
            # 每 2 个 checkpoint 产生 1~2 个语义事件
            if cp % 2 == 0:
                evo_events.append(_evo(t, cp, "parameter_change", counter, ts()))
                if cp % 4 == 0:
                    evo_events.append(_evo(t, cp, "bug_fix", counter, ts()))
                    evo_events.append(_evo(t, cp, "decision", counter, ts()))
                    evo_events.append(_evo(t, cp, "feature_change", counter, ts()))
        counter += 1
        raw_events.append(
            RawEvent(
                id=f"RAW-{counter:022d}",
                project_id=PRJ,
                timestamp=ts(),
                kind="task_completed",
                source="perf",
                task_id=task_id,
                payload={"summary": "done", "final_ref": "x" * 40},
            )
        )

    # 分批追加（每批一个 analysis batch，analysis_key 批内唯一）
    for start in range(0, len(raw_events), 2000):
        raw.append_many(raw_events[start : start + 2000])
    for start in range(0, len(evo_events), 500):
        evo.append_many(evo_events[start : start + 500])
    return len(raw_events), len(evo_events)


_EVO_PAYLOADS = {
    "parameter_change": lambda t, cp: {
        "name": f"Param{t % 17}", "before": str(cp), "after": str(cp + 1)
    },
    "bug_fix": lambda t, cp: {
        "problem": f"问题 {t}-{cp}", "fix": "修复", "affected_scope": ["Core/HotFile.cs"]
    },
    "decision": lambda t, cp: {
        "subject": f"主题 {t % 23}", "decision": f"决定 {t}-{cp}"
    },
    "feature_change": lambda t, cp: {"feature": f"功能 {t % 31}", "action": "modified"},
}

_evo_seq = 0


def _evo(t: int, cp: int, etype: str, salt: int, timestamp: datetime) -> EvolutionEvent:
    global _evo_seq
    _evo_seq += 1
    return EvolutionEvent(
        id=f"EVO-{_evo_seq:023d}",
        project_id=PRJ,
        task_id=f"TASK-T{t:04d}",
        checkpoint_id=f"CHK-T{t:04d}C{cp:03d}",
        timestamp=timestamp,
        type=etype,  # type: ignore[arg-type]
        title=f"{etype} {t}-{cp}",
        description="",
        evidence_ids=[f"EVD-{salt:021d}"],
        analyzer_version="perf",
        prompt_version="perf",
        analysis_batch_id=f"ANL-{salt:021d}",
        analysis_key=f"{PRJ}|perf|{salt}|{_evo_seq}",
        payload=_EVO_PAYLOADS[etype](t, cp),
    )


def test_10k_events_rebuild_and_query_performance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    raw_count, evo_count = generate_ledgers(project)
    assert raw_count >= 10_000, f"测试数据量不足: {raw_count}"
    assert evo_count >= 1_000, f"语义事件量不足: {evo_count}"

    started = time.perf_counter()
    report = rebuild_projection(project)
    rebuild_s = time.perf_counter() - started
    print(f"\nraw={raw_count} evo={evo_count} rebuild={rebuild_s:.2f}s")
    assert rebuild_s < REBUILD_BUDGET_S, f"rebuild took {rebuild_s:.2f}s"
    assert report.counts["raw_events"] == raw_count
    assert report.counts["evolution_events"] == evo_count
    assert report.counts["tasks"] == TASKS
    assert report.counts["checkpoints"] == TASKS * CHECKPOINTS_PER_TASK

    service = QueryService(project)
    budgets: list[tuple[str, float]] = []

    def measure(label: str, fn) -> tuple[float, object]:
        t0 = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - t0
        budgets.append((label, elapsed))
        return elapsed, result

    elapsed, _ = measure("task_history", lambda: service.get_task_history("TASK-T0119"))
    assert elapsed < QUERY_BUDGET_S, f"task query took {elapsed:.3f}s"
    assert _ is not None

    elapsed, history = measure(
        "parameter_history", lambda: service.get_parameter_history("Param5")
    )
    assert elapsed < QUERY_BUDGET_S, f"parameter query took {elapsed:.3f}s"
    assert len(history.changes) > 50

    elapsed, state = measure("project_state", lambda: service.get_project_state())
    assert elapsed < QUERY_BUDGET_S, f"project state took {elapsed:.3f}s"
    assert state.current_parameters  # 归约出当前参数

    elapsed, hot = measure("file_history", lambda: service.get_file_history("Core/HotFile.cs"))
    assert elapsed < QUERY_BUDGET_S, f"file history took {elapsed:.3f}s"
    assert len(hot.file_changes) == TASKS * CHECKPOINTS_PER_TASK

    elapsed, bugs = measure("bug_history", lambda: service.get_bug_history(scope="Core/HotFile.cs"))
    assert elapsed < QUERY_BUDGET_S, f"bug history took {elapsed:.3f}s"
    assert len(bugs) > 100

    for label, elapsed in budgets:
        print(f"  {label}: {elapsed * 1000:.1f}ms")
