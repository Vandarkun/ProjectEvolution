"""工作区快照与增量计算测试（06-git-integration.md §5，phase-04 §6/§7/§10）。

核心场景：
- Task 前已有 Dirty Tree 不被算成 Task 新变化；
- 两个 Checkpoint 之间不 commit，增量仍正确；
- rename（hash 相同 / 内容相近）可识别；
- 提交发生后快照算法依旧正确；
- 文件恢复为 HEAD 状态的边界情形。
"""

from __future__ import annotations

from pathlib import Path

from tests.gitrepo import commit_all, init_repo

from project_evolution.git import (
    BlobStore,
    GitAdapter,
    SnapshotStore,
    capture_worktree_snapshot,
    compute_increment,
)


def snap(repo: Path, adapter: GitAdapter, task: str, checkpoint: str | None = None):
    blobs = BlobStore.for_project(repo)
    store = SnapshotStore(repo)
    return capture_worktree_snapshot(
        adapter, blobs, store, task_id=task, checkpoint_id=checkpoint
    )


def change_map(increment):
    return {change.path: change.change for change in increment.changes}


# ------------------------------------------------------------------ baseline


def test_clean_repo_snapshot_is_empty(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"a\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    snapshot, rel = snap(repo, adapter, "TASK-1")
    assert snapshot.files == {}
    assert snapshot.head is not None
    assert rel.endswith("TASK-1/start.json")
    assert SnapshotStore(repo).load(rel) is not None


def test_dirty_tree_not_attributed_to_task(tmp_path: Path) -> None:
    """phase-04 §10 场景 1-3：Task 前 A.cs 已改，Task 只改 B.cs。"""
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"old\n")
    (repo / "B.cs").write_bytes(b"b\n")
    commit_all(repo, "init")
    (repo / "A.cs").write_bytes(b"old-dirty\n")  # Task 前已有未提交变化
    adapter = GitAdapter.detect(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    assert set(base.files) == {"A.cs"}  # 基线快照记录了旧 dirty

    (repo / "B.cs").write_bytes(b"b changed\n")  # Task 中只修改 B.cs
    current, _ = snap(repo, adapter, "TASK-1", "CHK-1")
    increment = compute_increment(base, current, adapter=adapter, blobs=BlobStore.for_project(repo))

    assert change_map(increment) == {"B.cs": "modified"}  # A.cs 不被误归属
    b_change = increment.changes[0]
    assert b_change.old_hash != b_change.new_hash
    assert "-b" in b_change.diff_text and "+b changed" in b_change.diff_text


def test_increment_between_checkpoints_without_commit(tmp_path: Path) -> None:
    """phase-04 §10 场景 5：两个 checkpoint 之间不 commit，增量仍逐次正确。"""
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"a\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    (repo / "A.cs").write_bytes(b"a\na2\n")
    s1, _ = snap(repo, adapter, "TASK-1", "CHK-1")
    inc1 = compute_increment(base, s1, adapter=adapter, blobs=blobs)
    assert change_map(inc1) == {"A.cs": "modified"}
    assert inc1.changes[0].added_lines == 1

    (repo / "C.py").write_bytes(b"print('c')\n")
    s2, _ = snap(repo, adapter, "TASK-1", "CHK-2")
    inc2 = compute_increment(s1, s2, adapter=adapter, blobs=blobs)
    assert change_map(inc2) == {"C.py": "added"}  # A.cs 不重复计算
    assert inc2.changes[0].diff_text.startswith("diff --git")
    assert "--- /dev/null" in inc2.changes[0].diff_text


def test_added_modified_deleted(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"keep\n")
    (repo / "gone.txt").write_bytes(b"gone\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    (repo / "new.txt").write_bytes(b"new\n")  # added（untracked）
    (repo / "gone.txt").unlink()  # deleted
    (repo / "keep.txt").write_bytes(b"keep2\n")  # modified
    current, _ = snap(repo, adapter, "TASK-1", "CHK-1")

    inc = compute_increment(base, current, adapter=adapter, blobs=blobs)
    assert change_map(inc) == {
        "new.txt": "added",
        "gone.txt": "deleted",
        "keep.txt": "modified",
    }
    gone = next(c for c in inc.changes if c.path == "gone.txt")
    assert "+++ /dev/null" in gone.diff_text


def test_commit_between_checkpoints_still_correct(tmp_path: Path) -> None:
    """checkpoint 之间发生了 commit：快照算法基于工作区，依旧只报新变化。"""
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"a\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    (repo / "A.cs").write_bytes(b"a\nb\n")
    s1, _ = snap(repo, adapter, "TASK-1", "CHK-1")
    commit_all(repo, "commit work")  # Agent 中途提交
    (repo / "A.cs").write_bytes(b"a\nb\nc\n")  # 提交后继续改
    s2, _ = snap(repo, adapter, "TASK-1", "CHK-2")

    inc2 = compute_increment(s1, s2, adapter=adapter, blobs=blobs)
    assert change_map(inc2) == {"A.cs": "modified"}
    assert inc2.base_head != inc2.head  # HEAD 变化被记录
    assert "+c" in inc2.changes[0].diff_text


def test_reverted_to_head_edge_case(tmp_path: Path) -> None:
    """上一快照有差异、本快照恢复为 HEAD 状态：仍按快照对比报一次修改。"""
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_bytes(b"base\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    (repo / "A.cs").write_bytes(b"temp\n")
    s1, _ = snap(repo, adapter, "TASK-1", "CHK-1")
    (repo / "A.cs").write_bytes(b"base\n")  # 恢复原状（与 HEAD 一致）
    s2, _ = snap(repo, adapter, "TASK-1", "CHK-2")
    inc = compute_increment(s1, s2, adapter=adapter, blobs=blobs)
    assert change_map(inc) == {"A.cs": "modified"}
    assert "-temp" in inc.changes[0].diff_text and "+base" in inc.changes[0].diff_text


# --------------------------------------------------------------------- rename


def test_rename_exact_hash(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "old_service.py").write_bytes(b"def run():\n    return 1\n" * 5)
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    (repo / "new_service.py").write_bytes(b"def run():\n    return 1\n" * 5)
    (repo / "old_service.py").unlink()
    current, _ = snap(repo, adapter, "TASK-1", "CHK-1")

    inc = compute_increment(base, current, adapter=adapter, blobs=blobs)
    assert change_map(inc) == {"new_service.py": "renamed"}
    change = inc.changes[0]
    assert change.old_path == "old_service.py"
    assert change.new_path == "new_service.py"
    assert change.similarity == 1.0
    assert change.diff_text == ""  # 内容未变，无 diff 段


def test_rename_with_small_edit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    body = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(30))
    (repo / "old_service.py").write_bytes(body.encode())
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    edited = body.replace("return 29", "return 42")
    (repo / "new_service.py").write_bytes(edited.encode())
    (repo / "old_service.py").unlink()
    current, _ = snap(repo, adapter, "TASK-1", "CHK-1")

    inc = compute_increment(base, current, adapter=adapter, blobs=blobs)
    assert change_map(inc) == {"new_service.py": "renamed"}
    change = inc.changes[0]
    assert change.similarity is not None and 0.6 <= change.similarity < 1.0
    assert "-    return 29" in change.diff_text
    assert "+    return 42" in change.diff_text


def test_unrelated_add_delete_not_paired(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"print('a')\n")
    (repo / "b.txt").write_bytes(b"hello world\n")
    commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    blobs = BlobStore.for_project(repo)

    base, _ = snap(repo, adapter, "TASK-1")
    (repo / "c.md").write_bytes(b"# doc\n")
    (repo / "b.txt").unlink()
    current, _ = snap(repo, adapter, "TASK-1", "CHK-1")
    inc = compute_increment(base, current, adapter=adapter, blobs=blobs)
    assert change_map(inc) == {"c.md": "added", "b.txt": "deleted"}


# ---------------------------------------------------------------- blob store


def test_blob_store_dedup(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    h1 = blobs.write(b"same")
    h2 = blobs.write(b"same")
    assert h1 == h2
    assert blobs.read(h1) == b"same"
    assert blobs.path_for(h1).name == f"blob-{h1}"
