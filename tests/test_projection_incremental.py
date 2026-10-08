"""增量投影测试（phase-06 §7：rebuild 结果 == incremental 结果）。

流程：边写账本边 update_projection，随后把 Canonical 数据复制到另一个
项目根做 full rebuild，逐表比较全部行——两者必须完全一致。
另覆盖：首次自动 rebuild、up-to-date 幂等、失败标记 stale 且水位不推进。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.config import PEL_DIR_NAME
from project_evolution.evolution import (
    AnalyzerResponse,
    CandidateEvent,
    EvolutionAnalyzerService,
    MockProvider,
)
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.index import (
    ProjectionError,
    ProjectionStore,
    rebuild_projection,
    update_projection,
)
from project_evolution.index.schema import PROJECTION_TABLES
from project_evolution.ledger import EvolutionEventLedger
from project_evolution.models import EvolutionEvent

PRJ = "PRJ-incremental"


def build_incremental_history(tmp_path: Path):
    """构造跨多个阶段的事件流，每个阶段后执行一次 update_projection。"""
    repo = init_repo(tmp_path)
    hook = repo / "Core" / "MouseHook.cs"
    hook.parent.mkdir()
    hook.write_text("class MouseHook { private const int PollMs = 16; }\n", encoding="utf-8")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)

    # 阶段 1：无 DB 时 update -> 自动 full rebuild（空投影）
    report = update_projection(repo)
    assert report.status == "rebuilt"

    task = services.tasks.start_task(PRJ, "Hook 性能优化", agent="codex")
    report = update_projection(repo)
    assert report.status == "updated" and report.applied_raw_events >= 2

    hook.write_text(
        "class MouseHook { private const int PollMs = 8; }\n", encoding="utf-8"
    )
    checkpoint1 = services.checkpoints.create_checkpoint(task.id, summary="PollMs 16 -> 8")

    def respond(request) -> AnalyzerResponse:
        evidence = [entry.id for entry in request.digest.evidence]
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="PollMs 16 -> 8",
                    description="降低轮询间隔",
                    evidence_ids=evidence[:1],
                    payload={"name": "PollMs", "before": "16", "after": "8"},
                ),
                CandidateEvent(
                    type="bug_fix",
                    title="轮询忙等",
                    description="改为事件驱动",
                    evidence_ids=evidence[:1],
                    payload={
                        "problem": "轮询忙等浪费 CPU",
                        "fix": "事件驱动等待",
                        "affected_scope": ["MouseHook.cs"],
                    },
                ),
            ]
        )

    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=respond))
    outcome = analyzer.analyze_checkpoint(task.id, checkpoint1.id)
    assert outcome.status == "committed" and len(outcome.events) == 2

    report = update_projection(repo)
    assert report.status == "updated" and report.applied_evolution_events == 2

    # 阶段 4：第二个 checkpoint（含新文件 + rename）+ 更多语义事件
    hook.write_text(
        "class MouseHook { private const int PollMs = 4; }\n", encoding="utf-8"
    )
    extra = repo / "Core" / "HookConfig.cs"
    extra.write_text("class HookConfig { }\n", encoding="utf-8")
    renamed = repo / "Core" / "HookOptions.cs"
    renamed.write_text("class HookOptions { }\n", encoding="utf-8")
    commit_all(repo, "stage4")
    (repo / "Core" / "HookOptions.cs").unlink()
    (repo / "Core" / "HookConfig.cs").rename(repo / "Core" / "HookOptions.cs")
    checkpoint2 = services.checkpoints.create_checkpoint(task.id, summary="继续调优")

    def respond2(request) -> AnalyzerResponse:
        evidence = [entry.id for entry in request.digest.evidence]
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="PollMs 8 -> 4",
                    description="进一步降低轮询间隔",
                    evidence_ids=evidence[:1],
                    payload={"name": "PollMs", "before": "8", "after": "4"},
                ),
                CandidateEvent(
                    type="decision",
                    title="Hook 层不引入依赖",
                    description="保持 stdlib",
                    evidence_ids=evidence[:1],
                    payload={"subject": "Hook 依赖策略", "decision": "零第三方依赖"},
                ),
                CandidateEvent(
                    type="feature_change",
                    title="Hook 配置对象",
                    description="新增配置",
                    evidence_ids=evidence[:1],
                    payload={"feature": "Hook 配置对象", "action": "added"},
                ),
            ]
        )

    outcome2 = EvolutionAnalyzerService(
        repo, MockProvider(responder=respond2)
    ).analyze_checkpoint(task.id, checkpoint2.id)
    assert outcome2.status == "committed" and len(outcome2.events) == 3

    report = update_projection(repo)
    assert report.status == "updated" and report.applied_evolution_events == 3

    # 阶段 5：Task 收尾 + Session 结束
    services.tasks.complete_task(task.id, summary="性能达标")
    report = update_projection(repo)
    assert report.status == "updated"
    return repo


def dump_tables(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in PROJECTION_TABLES
        }
    finally:
        conn.close()


def copy_canonical(src: Path, dst: Path) -> Path:
    """把 Canonical 数据（events/）复制到另一个项目根（不带 .runtime）。"""
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / PEL_DIR_NAME / "events", dst / PEL_DIR_NAME / "events")
    return dst


def test_incremental_projection_equals_full_rebuild(tmp_path: Path) -> None:
    """phase-06 §7 一致性：同一份 Ledger，full rebuild == incremental。"""
    repo = build_incremental_history(tmp_path)

    incremental_store = ProjectionStore(repo / PEL_DIR_NAME / ".runtime")
    incremental_db = incremental_store.db_path(incremental_store.current_version())

    twin = copy_canonical(repo, tmp_path / "twin")
    rebuilt = rebuild_projection(twin)
    rebuilt_db = Path(rebuilt.db_path)

    incremental_tables = dump_tables(incremental_db)
    rebuilt_tables = dump_tables(rebuilt_db)
    assert set(incremental_tables) == set(rebuilt_tables)
    for table in PROJECTION_TABLES:
        assert sorted(incremental_tables[table]) == sorted(rebuilt_tables[table]), (
            f"table {table} differs between incremental and rebuild"
        )

    # 水位追平 Ledger head
    conn = sqlite3.connect(incremental_db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM projection_meta"))
    finally:
        conn.close()
    last_raw_seq = max(
        int(line.split('"seq":', 1)[1].split(",", 1)[0])
        for line in (repo / PEL_DIR_NAME / "events" / "raw.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert meta["raw_seq"] == str(last_raw_seq)
    assert meta["stale"] == "0"


def test_update_projection_is_idempotent_when_up_to_date(tmp_path: Path) -> None:
    repo = build_incremental_history(tmp_path)
    first = update_projection(repo)
    assert first.status == "up-to-date"
    assert first.applied_raw_events == 0 and first.applied_evolution_events == 0
    second = update_projection(repo)
    assert second.status == "up-to-date"


def test_update_after_db_deleted_rebuilds(tmp_path: Path) -> None:
    repo = build_incremental_history(tmp_path)
    store = ProjectionStore(repo / PEL_DIR_NAME / ".runtime")
    version = store.current_version()
    store.close()
    for suffix in ("", "-wal", "-shm"):
        store.db_path(version).with_name(store.db_path(version).name + suffix).unlink(
            missing_ok=True
        )
    store.pointer_path.unlink()
    report = update_projection(repo)
    assert report.status == "rebuilt"
    assert report.counts["evolution_events"] == 5


def test_failed_update_marks_stale_and_keeps_watermark(tmp_path: Path) -> None:
    """非法语义事件 -> 更新失败：水位不推进、DB 标记 stale、账本不动。"""
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    commit_all(repo, "init")
    update_projection(repo)  # 空 DB

    evo = EvolutionEventLedger(repo)
    evo.append_many(
        [
            EvolutionEvent(
                id="EVO-GOOD000000000000000000000",
                project_id=PRJ,
                task_id=None,
                checkpoint_id=None,
                timestamp=datetime.now(timezone.utc),
                type="parameter_change",
                title="正常事件",
                description="",
                evidence_ids=[],
                analyzer_version="test",
                prompt_version="test",
                analysis_batch_id="ANL-ok",
                analysis_key="PRJ|ok",
                payload={"name": "P", "before": "1", "after": "2"},
            )
        ]
    )
    report = update_projection(repo)
    assert report.applied_evolution_events == 1

    # 追加一个 payload 非法的事件（缺 name）
    evo.append_many(
        [
            EvolutionEvent(
                id="EVO-BAD0000000000000000000000",
                project_id=PRJ,
                task_id=None,
                checkpoint_id=None,
                timestamp=datetime.now(timezone.utc) + timedelta(seconds=1),
                type="parameter_change",
                title="坏事件",
                description="",
                evidence_ids=[],
                analyzer_version="test",
                prompt_version="test",
                analysis_batch_id="ANL-bad",
                analysis_key="PRJ|bad",
                payload={"before": "2", "after": "3"},
            )
        ]
    )
    ledger_bytes = (repo / PEL_DIR_NAME / "events" / "evolution.jsonl").read_bytes()

    with pytest.raises(ProjectionError, match="invalid payload"):
        update_projection(repo)

    # Canonical Ledger 未被修改（绝不回滚真实历史）
    assert (repo / PEL_DIR_NAME / "events" / "evolution.jsonl").read_bytes() == ledger_bytes

    store = ProjectionStore(repo / PEL_DIR_NAME / ".runtime")
    conn = sqlite3.connect(store.db_path(store.current_version()))
    try:
        meta = dict(conn.execute("SELECT key, value FROM projection_meta"))
    finally:
        conn.close()
    assert meta["stale"] == "1"
    # 水位停留在最后成功应用的事件
    assert meta["evo_seq"] == "1"
    # 成功事件仍在，坏事件未入库
    rows = dump_tables(store.db_path(store.current_version()))["parameter_changes"]
    assert [row[0] for row in rows] == ["EVO-GOOD000000000000000000000"]

    # 修复（手工删除坏行）后 update 成功并清除 stale
    lines = (repo / PEL_DIR_NAME / "events" / "evolution.jsonl").read_text(
        encoding="utf-8"
    ).splitlines(keepends=True)
    (repo / PEL_DIR_NAME / "events" / "evolution.jsonl").write_text(
        "".join(line for line in lines if "EVO-BAD" not in line), encoding="utf-8"
    )
    report = update_projection(repo)
    assert report.status == "up-to-date"
    conn = sqlite3.connect(store.db_path(store.current_version()))
    try:
        assert dict(conn.execute("SELECT key, value FROM projection_meta"))["stale"] == "0"
    finally:
        conn.close()
