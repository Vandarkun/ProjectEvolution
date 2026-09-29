"""测试辅助：创建隔离配置的临时 Git 仓库。

GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM 指向空文件，避免宿主机全局配置
（autocrlf、init.templateDir、hook 等）影响测试行为。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git_env(repo: Path) -> dict[str, str]:
    empty = repo.parent / "_empty_gitconfig"
    if not empty.exists():
        empty.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_SYSTEM": str(empty),
    }


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        env=_git_env(repo),
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {args} failed (exit={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace')}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    return repo


def commit_all(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(
        repo,
        "-c",
        "user.name=pytest",
        "-c",
        "user.email=pytest@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )
    return run_git(repo, "rev-parse", "HEAD").strip()


def raw_status(repo: Path) -> list[str]:
    """不过滤的 git status --porcelain -uall（用于证明 PEL 自排除确实在过滤）。"""
    out = run_git(repo, "status", "--porcelain=v1", "-z", "-uall")
    return [token[3:] for token in out.split("\0") if token]
