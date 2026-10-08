"""CLI 兼容入口测试（phase-07 §3，09-agent-adapters.md §6）。

``pel task start/checkpoint/complete`` 供只支持 Shell 的 Agent 使用，
构建在与 MCP 相同的 Tool Service 层上；输出 JSON、错误稳定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.cli import main


def run_cli(argv: list[str]) -> tuple[int, str]:
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue() + stderr.getvalue()


def test_task_start_checkpoint_complete_roundtrip(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    commit_all(repo, "init")

    code = main(["task", "start", "增加窗口透明度", "--project", str(repo), "--agent", "codex"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    assert started["task_id"].startswith("TASK-")
    assert started["status"] == "active"
    assert started["baseline"]
    assert started["session_id"].startswith("SES-")

    (repo / "app.py").write_text("x = 2\n")
    code = main(
        [
            "task", "checkpoint", started["task_id"],
            "--project", str(repo),
            "--summary", "透明度服务完成",
            "--reason", "milestone",
            "--test", "dotnet build=passed",
            "--test", "pytest -q=failed",
        ]
    )
    assert code == 0
    checkpoint = json.loads(capsys.readouterr().out)
    assert checkpoint["checkpoint_saved"] is True
    assert checkpoint["analysis_status"] == "disabled"  # CLI 入口不组装 worker
    assert checkpoint["evidence"]["changed_files"] == ["app.py"]

    code = main(
        ["task", "complete", started["task_id"], "--project", str(repo), "--summary", "done"]
    )
    assert code == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    assert completed["checkpoint_id"].startswith("CHK-")

    # 事实落在账本：test 提示事件可见
    from project_evolution.git.collector import create_git_aware_task_services

    services = create_git_aware_task_services(repo, project_id=started["project"]["id"])
    kinds = [event.kind for event in services.store.replay()]
    assert kinds.count("test_passed") == 1
    assert kinds.count("test_failed") == 1
    assert "task_completed" in kinds


def test_task_checkpoint_unknown_task_errors_stably(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    code = main(["task", "checkpoint", "TASK-404", "--project", str(repo)])
    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "TASK_NOT_FOUND"
    assert payload["recoverable"] is True


def test_task_start_missing_root_fails(tmp_path: Path) -> None:
    code, _ = run_cli(["task", "start", "t", "--project", str(tmp_path / "nope")])
    assert code == 2


def test_task_bad_test_hint_rejected(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    commit_all(repo, "init")
    code = main(["task", "start", "t", "--project", str(repo)])
    assert code == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]
    with pytest.raises(SystemExit):
        main(["task", "checkpoint", task_id, "--project", str(repo), "--test", "bad"])


def test_agent_instructions_command(capsys) -> None:
    code = main(["mcp", "agent-instructions"])
    assert code == 0
    text = capsys.readouterr().out
    for token in ("start_task", "checkpoint_task", "complete_task", "get_history"):
        assert token in text
    assert "not call these tools every turn" in text  # 明确不要求每轮调用
    assert ".project-evolution/events" in text  # 禁止手改账本


def test_agent_instructions_with_project_hint(capsys) -> None:
    main(["mcp", "agent-instructions", "--project", "screepin"])
    assert "project=screepin" in capsys.readouterr().out
