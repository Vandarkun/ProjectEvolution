"""Checkpoint 事实提示（phase-07，08-mcp-integration.md §5）核心测试。

Agent 提供的 tests / agent_summary / changed-file hints 必须与
checkpoint_created 同批（同写锁、连续 seq）落盘，使随后触发的
后台语义分析重放 Ledger 时一定看得到它们。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.errors import ValidationError
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.models import RawEvent

PRJ = "PRJ-test"


def checkpoint_events(services, checkpoint_id: str) -> list[RawEvent]:
    return [
        event
        for event in services.store.replay()
        if event.checkpoint_id == checkpoint_id
    ]


def test_hint_events_persisted_in_same_batch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "app.py").write_bytes(b"v2\n")

    cp = services.checkpoints.create_checkpoint(
        task.id,
        summary="window opacity done",
        reason="milestone",
        tests=[
            {"command": "dotnet build", "status": "passed"},
            {"command": "pytest -q", "status": "failed"},
        ],
        agent_summary="window opacity done",
        agent_changed_files=["Core/WindowOpacityService.cs"],
    )

    events = checkpoint_events(services, cp.id)
    kinds = [event.kind for event in events]
    assert kinds[0] == "checkpoint_created"
    assert kinds[1] == "file_modified"  # Git 确定性采集在前
    assert kinds.count("test_passed") == 1
    assert kinds.count("test_failed") == 1
    assert kinds.count("agent_summary") == 1
    # 同批连续 seq：提示事件紧随 checkpoint 事件，无其他 writer 可插入
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs)
    assert seqs[-1] - seqs[0] == len(seqs) - 1

    created = events[0]
    assert created.payload["agent_changed_files"] == ["Core/WindowOpacityService.cs"]
    passed = next(e for e in events if e.kind == "test_passed")
    assert passed.payload == {"command": "dotnet build", "source": "agent_claim"}
    failed = next(e for e in events if e.kind == "test_failed")
    assert failed.payload["command"] == "pytest -q"
    summary = next(e for e in events if e.kind == "agent_summary")
    assert summary.payload == {"summary": "window opacity done"}

    # 归属：都挂在同一 task / checkpoint 上
    assert all(event.task_id == task.id for event in events)


def test_hints_reach_analyzer_digest(tmp_path: Path) -> None:
    """提示事件必须进入 ChangeDigest（模型输入）——重放可见性的端到端证明。"""
    from project_evolution.git.collector import build_digest_for_checkpoint

    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "app.py").write_bytes(b"v2\n")
    cp = services.checkpoints.create_checkpoint(
        task.id,
        summary="step one",
        agent_summary="step one",
        tests=[{"command": "make test", "status": "passed"}],
    )

    from project_evolution.evolution.analyzer_service import (
        EvolutionAnalyzerService,
    )
    from project_evolution.evolution.providers import MockProvider

    analyzer = EvolutionAnalyzerService(
        repo, MockProvider(), task_store=services.store
    )
    request, _context = analyzer._prepare(task.id, cp.id)
    digest = request.digest
    assert digest.test_results[0].command == "make test"
    assert digest.test_results[0].status == "passed"
    assert digest.agent_summaries[0].text == "step one"
    evidence_ids = {entry.id for entry in digest.evidence}
    assert any(eid.startswith("RAW-") for eid in evidence_ids)  # 提示事件是 Evidence

    # build_digest_for_checkpoint 不包含提示（它只重建 Git 增量），由 _prepare 合并
    git_digest = build_digest_for_checkpoint(
        repo, project_id=PRJ, task_id=task.id, checkpoint_id=cp.id
    )
    assert git_digest.test_results == []


def test_invalid_test_hint_rejected_before_write(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")

    with pytest.raises(ValidationError):
        services.checkpoints.create_checkpoint(
            task.id, tests=[{"command": "x", "status": "skipped"}]
        )
    with pytest.raises(ValidationError):
        services.checkpoints.create_checkpoint(task.id, tests=[{"command": "", "status": "passed"}])
    # 校验失败不产生任何 checkpoint 事件
    assert all(event.kind != "checkpoint_created" for event in services.store.replay())


def test_empty_hints_keep_checkpoint_shape(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "a.py").write_bytes(b"a2\n")
    cp = services.checkpoints.create_checkpoint(task.id, summary="plain")
    created = next(
        e for e in services.store.replay() if e.kind == "checkpoint_created"
    )
    assert "agent_changed_files" not in created.payload
    kinds = [e.kind for e in checkpoint_events(services, cp.id)]
    assert "test_passed" not in kinds and "agent_summary" not in kinds
