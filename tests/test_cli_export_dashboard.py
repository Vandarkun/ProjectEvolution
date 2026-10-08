"""pel export / pel dashboard CLI 测试（phase-08 §7，11 §6）。

- export markdown/html：stdout 与 --output 文件两种去向；内容带 Event ID；
- export 非项目目录报错退出 2；
- dashboard：参数校验（不实际启动 server）；缺 extra 时给出安装提示。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_evolution.cli import main
from test_query_service import seed_event_stream  # noqa: F401 - 复用种子


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    seed_event_stream(root)
    return root


def test_cli_export_markdown_to_stdout(project: Path, capsys) -> None:
    code = main(["export", "--project", str(project), "--format", "markdown"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Project Evolution Report")
    assert "[EVO-" in out  # Event ID 溯源
    assert "## 5. Parameter Evolution" in out


def test_cli_export_html_to_file(project: Path, tmp_path: Path, capsys) -> None:
    target = tmp_path / "reports" / "evolution.html"
    code = main(
        ["export", "--project", str(project), "--format", "html", "--output", str(target)]
    )
    assert code == 0
    assert f"report written: {target}" in capsys.readouterr().out
    html = target.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Evolution Report" in html


def test_cli_export_rejects_missing_project(tmp_path: Path, capsys) -> None:
    code = main(["export", "--project", str(tmp_path / "nope")])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_dashboard_argument_validation(project: Path, capsys) -> None:
    """dashboard 参数错误路径（不启动 server）：非项目目录直接退出 2。"""
    code = main(["dashboard", "--project", str(project.parent / "missing")])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_dashboard_missing_extra_message(project: Path, capsys, monkeypatch) -> None:
    """缺 fastapi 时给出安装指引（而不是 traceback）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    code = main(
        [
            "dashboard",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            "8642",
        ]
    )
    assert code == 2
    assert "project-evolution[dashboard]" in capsys.readouterr().err
