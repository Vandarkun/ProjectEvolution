"""服务级 Git Evidence 集成测试（phase-04 §6/§7/§10/§12）。

完整覆盖 phase-04 §10 Dirty Tree 测试矩阵：
1. Task 前已有 A.cs 未提交变化；
2. Task 中只修改 B.cs；
3. Checkpoint 不把 A.cs 旧变化当成本任务新增；
4. PEL 自身写入 .project-evolution/（事件、快照、patch）不出现在
   changed files / diff evidence / Digest 中；
5. 两个 Checkpoint 之间不 commit，增量仍正确。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.gitrepo import commit_all, init_repo, raw_status

from project_evolution.git.collector import (
    build_digest_for_checkpoint,
    create_git_aware_task_services,
)
from project_evolution.models import RawEvent

PRJ = "PRJ-test"


def file_events(services, checkpoint_id: str | None = None) -> list[RawEvent]:
    return [
        event
        for event in services.store.replay()
        if event.kind in ("file_added", "file_modified", "file_deleted")
        and (checkpoint_id is None or event.checkpoint_id == checkpoint_id)
    ]


def cp_event(services, checkpoint_id: str) -> RawEvent:
    return next(
        e
        for e in services.store.replay()
        if e.kind == "checkpoint_created" and e.checkpoint_id == checkpoint_id
    )


# --------------------------------------------------- dirty tree 完整场景 §10


def test_dirty_tree_full_scenario(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"A original\n")
    (repo / "B.cs").write_bytes(b"B original\n")
    commit_all(repo, "init")

    # 1. Task 前已有 A.cs 未提交变化
    (repo / "A.cs").write_bytes(b"A dirty before task\n")

    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "task in dirty repo", agent="codex")
    assert task.baseline_ref is not None
    started = next(e for e in services.store.replay() if e.kind == "task_started")
    assert started.payload["baseline_ref"] == task.baseline_ref
    assert started.payload["baseline_degraded"] is False
    assert started.payload["baseline_snapshot_path"]

    # 2. Task 中只修改 B.cs
    (repo / "B.cs").write_bytes(b"B changed in task\n")

    # 3 & 4. checkpoint：A.cs 不得出现；PEL 自身写入不得出现
    cp1 = services.checkpoints.create_checkpoint(task.id, summary="cp1", reason="step")
    cp1_events = file_events(services, cp1.id)
    assert [e.payload["path"] for e in cp1_events] == ["B.cs"]
    assert all(e.kind == "file_modified" for e in cp1_events)
    assert cp1.base_ref == cp1.head_ref == task.baseline_ref
    assert cp1_events[0].payload["evidence_id"].startswith("EVD-")

    # PEL 确实写脏了工作区（事件、快照、patch 都在 .project-evolution/ 下）
    pel_writes = [p for p in raw_status(repo) if p.startswith(".project-evolution/")]
    assert pel_writes, "PEL 应已写入自身数据目录"
    # 但 changed files / status 采集中永不出现
    for event in file_events(services):
        assert not event.payload["path"].startswith(".project-evolution/")

    # diff evidence 可追溯：patch 文件存在且 sha256 匹配
    ev1 = cp_event(services, cp1.id)
    evidence_id = ev1.payload["diff_evidence_id"]
    patch_rel = f".project-evolution/evidence/diffs/{evidence_id}.patch"
    patch = repo / patch_rel
    assert patch.is_file()
    sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    assert ev1.payload["diff_evidence_sha256"] == sha  # 内容寻址可追溯
    # patch 内包含 B.cs 的 diff，不含 A.cs 与 .project-evolution
    patch_text = patch.read_text(encoding="utf-8")
    assert "+B changed in task" in patch_text
    assert "A dirty" not in patch_text
    assert ".project-evolution" not in patch_text.replace("# ", "")

    # 5. 两个 checkpoint 之间不 commit，继续改新文件 C.py
    (repo / "C.py").write_bytes(b"print('new')\n")
    cp2 = services.checkpoints.create_checkpoint(task.id, summary="cp2")
    cp2_events = file_events(services, cp2.id)
    assert [e.payload["path"] for e in cp2_events] == ["C.py"]  # 只报增量
    assert cp2.index == 2

    # digest 重建同样干净
    digest = build_digest_for_checkpoint(
        repo, project_id=PRJ, task_id=task.id, checkpoint_id=cp2.id
    )
    assert digest.changed_files == ["C.py"]
    assert digest.added_files == ["C.py"]
    assert digest.scope_confidence == "normal"
    assert "A dirty" not in digest.summary

    services.tasks.complete_task(task.id, summary="done")
    completed = next(e for e in services.store.replay() if e.kind == "task_completed")
    assert completed.task_id == task.id


# --------------------------------------------------------- PEL 自排除 / 事件


def test_pel_writes_never_in_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    for i in range(3):
        (repo / "app.py").write_bytes(f"v{i + 2}\n".encode())
        services.checkpoints.create_checkpoint(task.id, summary=f"cp{i}")
    assert any(p.startswith(".project-evolution/") for p in raw_status(repo))
    for event in file_events(services):
        assert event.payload["path"] == "app.py"
    # 快照文件里的 files 映射也不含 PEL 目录
    import json

    snapshots = sorted((repo / ".project-evolution" / "evidence" / "snapshots").rglob("*.json"))
    assert snapshots
    for snap_file in snapshots:
        data = json.loads(snap_file.read_text(encoding="utf-8"))
        assert all(not p.startswith(".project-evolution/") for p in data["files"])


def test_secret_content_never_persisted_in_patch(tmp_path: Path) -> None:
    """secret 文件变化仍可被记录（path/hash），但内容永不写入 patch。"""
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    (repo / ".env").write_bytes(b"API_KEY=old\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / ".env").write_bytes(b"API_KEY=super-secret-value\n")
    cp = services.checkpoints.create_checkpoint(task.id)

    events = file_events(services, cp.id)
    assert [e.payload["path"] for e in events] == [".env"]  # 变化本身被记录
    event = cp_event(services, cp.id)
    patch = (
        repo / ".project-evolution" / "evidence" / "diffs"
        / (event.payload["diff_evidence_id"] + ".patch")
    )
    text = patch.read_text(encoding="utf-8")
    assert ".env" in text  # 事实可追溯
    assert "super-secret-value" not in text  # 内容绝不落盘
    assert "content withheld" in text


def test_no_changes_checkpoint_has_no_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    cp = services.checkpoints.create_checkpoint(task.id)
    event = cp_event(services, cp.id)
    assert "diff_evidence_id" not in event.payload  # 无变化不落空 patch
    assert file_events(services) == []
    diffs_dir = repo / ".project-evolution" / "evidence" / "diffs"
    assert not any(diffs_dir.glob("*.patch"))


def test_rename_via_services(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    body = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(20))
    (repo / "old.py").write_bytes(body.encode())
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "new.py").write_bytes(body.encode())
    (repo / "old.py").unlink()
    cp = services.checkpoints.create_checkpoint(task.id)
    events = file_events(services, cp.id)
    assert len(events) == 1
    assert events[0].kind == "file_modified"
    assert events[0].payload["old_path"] == "old.py"
    assert events[0].payload["new_path"] == "new.py"
    assert events[0].payload["change"] == "renamed"
    assert events[0].payload["similarity"] == 1.0


def test_snapshot_chain_missing_marks_low_confidence(tmp_path: Path) -> None:
    """快照被清理：必须显式 scope_confidence=low，不假装精确（06 §5）。"""
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")

    import shutil

    shutil.rmtree(repo / ".project-evolution" / "evidence" / "snapshots")
    (repo / "a.py").write_bytes(b"a2\n")
    cp = services.checkpoints.create_checkpoint(task.id)
    event = cp_event(services, cp.id)
    assert event.payload["scope_confidence"] == "low"
    assert "diff_evidence_id" not in event.payload  # 无法可靠计算增量
    digest = build_digest_for_checkpoint(
        repo, project_id=PRJ, task_id=task.id, checkpoint_id=cp.id
    )
    assert digest.scope_confidence == "low"


# ------------------------------------------------------------- degraded mode


def test_non_git_repo_degraded_mode(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_bytes(b"a\n")
    services = create_git_aware_task_services(plain, project_id=PRJ)

    task = services.tasks.start_task(PRJ, "non-git task")
    assert task.baseline_ref is None  # 无 Git 基线
    started = next(e for e in services.store.replay() if e.kind == "task_started")
    assert started.payload["baseline_degraded"] is True

    (plain / "a.py").write_bytes(b"a2\n")
    cp = services.checkpoints.create_checkpoint(task.id, summary="cp")
    assert cp.index == 1
    event = cp_event(services, cp.id)
    assert event.payload["scope_confidence"] == "degraded"
    assert file_events(services) == []  # 没有 Git 证据，不伪造
    digest = build_digest_for_checkpoint(
        plain, project_id=PRJ, task_id=task.id, checkpoint_id=cp.id
    )
    assert digest.scope_confidence == "degraded"
    assert digest.changed_files == []
    services.tasks.complete_task(task.id)
