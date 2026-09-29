"""GitAdapter 测试（phase-04 §5）：只读安全、repo 探测、status 自排除、基础读取。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo, raw_status, run_git

from project_evolution.git import GitAdapter, GitError, GitSafetyError, is_excluded_path


def test_detect_git_and_non_git(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    adapter = GitAdapter.detect(repo)
    assert adapter is not None
    assert adapter.repo_root.resolve() == repo.resolve()

    plain = tmp_path / "plain"
    plain.mkdir()
    assert GitAdapter.detect(plain) is None  # 不自动 git init（06 §2）
    # git 仓库的子目录也应探测到同一个 toplevel
    sub = repo / "sub"
    sub.mkdir()
    assert GitAdapter.detect(sub).repo_root.resolve() == repo.resolve()


def test_read_only_whitelist(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    adapter = GitAdapter.detect(repo)
    assert adapter is not None
    for forbidden in (["add", "-A"], ["commit", "-m", "x"], ["reset", "--hard"],
                      ["checkout", "main"], ["clean", "-fd"], ["merge", "dev"],
                      ["tag", "v1"]):
        with pytest.raises(GitSafetyError, match="read-only whitelist"):
            adapter._run(forbidden)
    # 全局选项前缀不绕过白名单
    with pytest.raises(GitSafetyError):
        adapter._run(["-c", "foo.bar=1", "commit", "-m", "x"])
    # 只读命令正常
    assert adapter._run(["rev-parse", "--git-dir"]) is not None


def test_head_branch_status(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_text("a\n", encoding="utf-8")
    head = commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)

    assert adapter.get_head_sha() == head
    assert adapter.get_branch() is not None
    assert adapter.get_status() == []

    (repo / "A.cs").write_text("a2\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".project-evolution").mkdir()
    (repo / ".project-evolution" / "events").mkdir()
    (repo / ".project-evolution" / "events" / "raw.jsonl").write_text("{}\n", encoding="utf-8")

    # 原始 status 里有 PEL 目录（证明确有写入），adapter 输出必须排除
    assert any(p.startswith(".project-evolution/") for p in raw_status(repo))
    assert adapter.get_status() == ["A.cs", "new.py"]


def test_empty_repo_head_is_none(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    adapter = GitAdapter.detect(repo)
    assert adapter is not None
    assert adapter.get_head_sha() is None  # 无提交
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert adapter.get_status() == ["a.txt"]  # untracked 仍可采集


def test_diff_helpers_and_metadata(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "A.cs").write_text("one\n", encoding="utf-8")
    head1 = commit_all(repo, "c1")
    (repo / "A.cs").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "B.cs").write_text("b\n", encoding="utf-8")
    head2 = commit_all(repo, "c2")

    adapter = GitAdapter.detect(repo)
    diff = adapter.get_diff(base=f"{head1}..{head2}")
    assert "B.cs" in diff and "+two" in diff
    assert adapter.get_changed_files(base=f"{head1}..{head2}") == ["A.cs", "B.cs"]
    stat = adapter.get_diff_stat(base=f"{head1}..{head2}")
    assert "files changed" in stat or "file changed" in stat or "insertion" in stat

    meta = adapter.get_commit_metadata(head2)
    assert meta.sha == head2
    assert meta.author == "pytest"
    assert meta.subject == "c2"

    run_git(repo, "tag", "v0.1.0")
    tags = adapter.get_tags()
    assert ("v0.1.0", head2) in tags


def test_file_lookup_in_rev(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "dir").mkdir()
    (repo / "dir" / "A.cs").write_bytes(b"content\n")
    head = commit_all(repo, "init")
    adapter = GitAdapter.detect(repo)
    assert adapter.file_exists_in_rev(head, "dir/A.cs")
    assert not adapter.file_exists_in_rev(head, "missing.cs")
    assert adapter.show_file(head, "dir/A.cs") == b"content\n"
    with pytest.raises(GitError):
        adapter.show_file(head, "missing.cs")


def test_is_excluded_path() -> None:
    assert is_excluded_path(".project-evolution/events/raw.jsonl")
    assert is_excluded_path(".project-evolution")
    assert is_excluded_path(".project-evolution\\events\\raw.jsonl")
    assert not is_excluded_path("src/main.py")
    assert not is_excluded_path("lib.project-evolution/x")
