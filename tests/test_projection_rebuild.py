"""rebuild_projection 测试（phase-06 §4：先做 Rebuild）。

覆盖：

- 真实闭环（Git + TaskServices + MockProvider）产生的两条账本 -> 全部投影表；
- 删除 DB 与指针后可完整重建（按 seq 重放）；
- 重建切换：版本号单调递增、指针跟随、旧文件延迟清理；
- 重建失败（账本损坏 / 语义 payload 非法）指针不切换、旧库仍可读；
- 时间戳乱序时仍按 seq 排序投影。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.cli import main as cli_main
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
)
from project_evolution.ledger import EvolutionEventLedger, LedgerCorruptionError
from project_evolution.models import EvolutionEvent

PRJ = "PRJ-rebuild"


# ----------------------------------------------------------------- helpers


def build_closed_loop_project(tmp_path: Path):
    """Git 仓库 + Task/Checkpoint + 四类语义事件（完整闭环数据源）。"""
    repo = init_repo(tmp_path)
    service_file = repo / "Core" / "MouseHook.cs"
    service_file.parent.mkdir()
    service_file.write_text(
        "class MouseHook { private const double OpacityMin = 0.2; }\n", encoding="utf-8"
    )
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "增加窗口透明度", agent="codex")

    service_file.write_text(
        "class MouseHook { private const double OpacityMin = 0.3; }\n", encoding="utf-8"
    )
    checkpoint = services.checkpoints.create_checkpoint(
        task.id, summary="OpacityMin 0.2 -> 0.3"
    )

    def respond(request) -> AnalyzerResponse:
        evidence = [entry.id for entry in request.digest.evidence]
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="OpacityMin 0.2 -> 0.3",
                    description="提高最低可见度",
                    evidence_ids=evidence[:1],
                    payload={
                        "name": "OpacityMin",
                        "before": "0.2",
                        "after": "0.3",
                        "reason": "夜间可读性",
                    },
                ),
                CandidateEvent(
                    type="feature_change",
                    title="窗口透明度调节",
                    description="新增透明度能力",
                    evidence_ids=evidence[:1],
                    payload={
                        "feature": "窗口透明度调节",
                        "action": "added",
                        "after": "Alt+Wheel 调节",
                    },
                ),
                CandidateEvent(
                    type="decision",
                    title="透明度快捷键采用 Alt+Wheel",
                    description="备选 Ctrl+Shift+U 会被输入法吞掉",
                    evidence_ids=evidence[:1],
                    payload={
                        "subject": "透明度快捷键",
                        "decision": "Alt+Wheel",
                        "reason": "无输入法冲突",
                        "alternatives": ["Ctrl+Shift+U"],
                    },
                ),
                CandidateEvent(
                    type="bug_fix",
                    title="Hook 线程阻塞导致输入卡顿",
                    description="窗口查询移出 Hook 线程",
                    evidence_ids=evidence[:1],
                    payload={
                        "problem": "Hook 线程内做窗口查询导致输入卡顿",
                        "cause": "同步 Win32 调用阻塞 low-level hook",
                        "fix": "窗口查询移出 Hook 线程",
                        "affected_scope": ["MouseHook.cs"],
                    },
                ),
            ]
        )

    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=respond))
    outcome = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert outcome.status == "committed" and len(outcome.events) == 4
    services.tasks.complete_task(task.id, summary="透明度完成")
    return repo, task, checkpoint, outcome


def make_store(repo: Path) -> ProjectionStore:
    return ProjectionStore(repo / PEL_DIR_NAME / ".runtime")


def fetch_all(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [tuple(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


# ------------------------------------------------------------------- tests


def test_rebuild_projects_closed_loop_ledgers(tmp_path: Path) -> None:
    repo, task, checkpoint, _outcome = build_closed_loop_project(tmp_path)

    report = rebuild_projection(repo)

    assert report.previous_version == 0
    assert report.version == 1
    assert make_store(repo).current_version() == 1
    counts = report.counts
    assert counts["tasks"] == 1
    assert counts["checkpoints"] == 1
    assert counts["evolution_events"] == 4
    assert counts["parameter_changes"] == 1
    assert counts["feature_changes"] == 1
    assert counts["decisions"] == 1
    assert counts["bug_fixes"] == 1
    assert counts["file_changes"] == 1  # MouseHook.cs modified
    assert counts["evidence"] == 1  # checkpoint diff evidence
    assert counts["sessions"] == 1  # start_task(agent=...) 自动创建的 Session
    assert counts["task_sessions"] == 1

    # tasks 行来自 phase-03 投影（含 complete 后的 finished_at/final_ref）
    task_row = fetch_all(report.db_path, "SELECT * FROM tasks")[0]
    assert task_row[0] == task.id
    assert task_row[3] == "completed"
    assert task_row[5] is not None  # finished_at

    # checkpoints 行带 base/head_ref 与 diff evidence 关联
    checkpoint_row = fetch_all(
        Path(report.db_path), "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
        (checkpoint.id,),
    )[0]
    assert checkpoint_row[4] == 1  # checkpoint_index
    assert checkpoint_row[11]  # diff_evidence_id 非空

    # file_changes 行指向 changed file 与 evidence
    file_row = fetch_all(Path(report.db_path), "SELECT * FROM file_changes")[0]
    assert file_row[2] == "Core/MouseHook.cs"
    assert file_row[4] == "modified"
    assert file_row[6] == checkpoint_row[11]  # evidence_id

    # evidence 行记录 sha256 ref
    evidence_row = fetch_all(Path(report.db_path), "SELECT * FROM evidence")[0]
    assert evidence_row[2] == "git_diff"
    assert evidence_row[3]  # sha256

    # 语义表展开（normalized name / before/after）
    param = fetch_all(Path(report.db_path), "SELECT * FROM parameter_changes")[0]
    assert param[2] == "opacitymin"
    assert (param[5], param[6]) == ("0.2", "0.3")


def test_delete_db_and_pointer_then_rebuild(tmp_path: Path) -> None:
    """验收标准：删除数据库文件与指针后可完整重建（按 seq replay）。"""
    repo, _task, _checkpoint, _outcome = build_closed_loop_project(tmp_path)
    first = rebuild_projection(repo)

    store = make_store(repo)
    old_rows = fetch_all(Path(first.db_path), "SELECT event_id, seq FROM raw_events")
    store.close()
    for path in (
        store.db_path(first.version),
        store.db_path(first.version).with_name(store.db_path(first.version).name + "-wal"),
        store.db_path(first.version).with_name(store.db_path(first.version).name + "-shm"),
        store.pointer_path,
    ):
        path.unlink(missing_ok=True)
    assert store.current_version() == 0

    second = rebuild_projection(repo)
    assert second.version >= 1
    new_rows = fetch_all(Path(second.db_path), "SELECT event_id, seq FROM raw_events")
    assert new_rows == old_rows  # 与第一次完全一致（确定性重放）
    assert second.counts == first.counts


def test_rebuild_switches_version_and_cleans_old(tmp_path: Path) -> None:
    """重建切换：版本单调递增、指针跟随、旧文件被延迟清理。"""
    repo, _task, _checkpoint, _outcome = build_closed_loop_project(tmp_path)
    first = rebuild_projection(repo)
    second = rebuild_projection(repo)

    assert second.version > first.version
    store = make_store(repo)
    assert store.current_version() == second.version
    # 旧文件已被清理（无打开连接时立即成功）
    assert not store.db_path(first.version).exists()
    assert store.db_path(second.version).exists()
    # 数据一致
    assert second.counts == first.counts


def test_rebuild_failure_keeps_old_projection_readable(tmp_path: Path) -> None:
    """账本中间损坏 -> rebuild 抛错；指针不切换，旧投影仍可读。"""
    repo, _task, _checkpoint, _outcome = build_closed_loop_project(tmp_path)
    good = rebuild_projection(repo)
    good_rows = fetch_all(Path(good.db_path), "SELECT COUNT(*) FROM evolution_events")

    # 在 raw.jsonl 末尾追加一行"合法 JSON 但中间语义损坏"的行不可行（尾部）；
    # 直接写一行非法 JSON + 换行 -> 中间损坏判定留给后续，这里构造：
    # 倒数第二行后插入坏行，使文件存在"中间损坏"
    raw_path = repo / PEL_DIR_NAME / "events" / "raw.jsonl"
    lines = raw_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) >= 2
    raw_path.write_text(
        "".join(lines[:-1]) + "{not-json}\n" + lines[-1], encoding="utf-8"
    )

    with pytest.raises(LedgerCorruptionError):
        rebuild_projection(repo)

    store = make_store(repo)
    assert store.current_version() == good.version  # 指针未动
    assert fetch_all(Path(good.db_path), "SELECT COUNT(*) FROM evolution_events") == good_rows
    # 失败的半成品文件被清理（尽力而为；指针未指向它，残留也不影响正确性）
    assert not store.db_path(good.version + 1).exists()


def test_rebuild_rejects_invalid_semantic_payload(tmp_path: Path) -> None:
    """evolution 事件 payload 违反语义模型 -> ProjectionError，指针不切换。"""
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    commit_all(repo, "init")
    evo = EvolutionEventLedger(repo)
    bad = EvolutionEvent(
        id="EVO-BAD0000000000000000000000",
        project_id=PRJ,
        task_id=None,
        checkpoint_id=None,
        timestamp=datetime.now(timezone.utc),
        type="parameter_change",
        title="坏 payload",
        description="缺 name",
        evidence_ids=[],
        analyzer_version="test",
        prompt_version="test",
        analysis_batch_id="ANL-test",
        analysis_key="PRJ|bad",
        payload={"before": "1", "after": "2"},  # 缺 name
    )
    evo.append_many([bad])

    with pytest.raises(ProjectionError, match="invalid payload"):
        rebuild_projection(repo)
    store = make_store(repo)
    assert store.current_version() == 0  # 从未成功切换


def test_rebuild_orders_by_seq_not_timestamp(tmp_path: Path) -> None:
    """timestamp 乱序（时钟偏移）不影响投影顺序：唯一排序键是 seq。"""
    repo = tmp_path / "project"
    repo.mkdir()
    evo = EvolutionEventLedger(repo)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = []
    for i, offset in enumerate([3, 1, 2]):  # 故意乱序的 timestamp
        events.append(
            EvolutionEvent(
                id=f"EVO-SEQ{i:022d}",
                project_id=PRJ,
                task_id=None,
                checkpoint_id=None,
                timestamp=base + timedelta(hours=offset),
                type="parameter_change",
                title=f"v{i}",
                description="",
                evidence_ids=[],
                analyzer_version="test",
                prompt_version="test",
                analysis_batch_id="ANL-test",
                analysis_key=f"PRJ|k{i}",
                payload={"name": "P", "before": str(i), "after": str(i + 1)},
            )
        )
    stamped = evo.append_many(events)

    report = rebuild_projection(repo)
    rows = fetch_all(Path(report.db_path), "SELECT seq, after_value FROM parameter_changes")
    assert rows == [(event.seq, str(i + 1)) for i, event in enumerate(stamped)]


def test_rebuild_empty_project_creates_empty_projection(tmp_path: Path) -> None:
    repo = tmp_path / "empty-project"
    repo.mkdir()
    report = rebuild_projection(repo)
    assert all(count == 0 for count in report.counts.values())
    assert make_store(repo).current_version() == 1


def test_cli_index_rebuild(tmp_path: Path) -> None:
    repo, _task, _checkpoint, _outcome = build_closed_loop_project(tmp_path)
    rc = cli_main(["index", "rebuild", "--project", str(repo), "--json"])
    assert rc == 0
    pointer = (repo / PEL_DIR_NAME / ".runtime" / "current-db.txt").read_text(
        encoding="ascii"
    )
    assert int(pointer) == 1

    # 人类可读输出
    rc = cli_main(["index", "rebuild", "--project", str(repo)])
    assert rc == 0

    # 项目不存在 -> 2
    rc = cli_main(["index", "rebuild", "--project", str(tmp_path / "nope")])
    assert rc == 2


def test_projection_meta_watermarks(tmp_path: Path) -> None:
    """projection_meta 记录与 Ledger head 的同步标记（07 §13）。"""
    repo, _task, _checkpoint, _outcome = build_closed_loop_project(tmp_path)
    report = rebuild_projection(repo)
    meta = {
        row[0]: row[1]
        for row in fetch_all(Path(report.db_path), "SELECT key, value FROM projection_meta")
    }
    raw_lines = (repo / PEL_DIR_NAME / "events" / "raw.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    evo_lines = (repo / PEL_DIR_NAME / "events" / "evolution.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert meta["raw_seq"] == str(json.loads(raw_lines[-1])["seq"])
    assert meta["evo_seq"] == str(json.loads(evo_lines[-1])["seq"])
    assert meta["stale"] == "0"
    assert meta["schema_version"] == "1"
