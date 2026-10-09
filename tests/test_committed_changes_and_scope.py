"""回归测试：checkpoint 间 committed 变更可见性、项目 scope 与接口一致性。

覆盖三组修复：

1. **committed 变更可见（06 §4/§9）**：Agent 在两次快照之间 commit 后工作区
   变干净，被 commit 的改动必须进入增量 / diff evidence / digest，且产生
   归属该 Checkpoint 的 ``git_commit`` RawEvents；
2. **MCP ``get_history kind=file``**：走 File History 查询而不是误入 bug 分支；
3. **嵌套仓库 scope（06 §5）**：项目目录是外层仓库子目录时，采集限定在
   项目子树内，父仓库其余变更绝不混入；子树内 PEL 目录同样被排除；
4. 次要修复：项目显示名解析、未知实体 API 404、nosniff 响应头。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo, run_git

from project_evolution.git.adapter import GitAdapter
from project_evolution.git.collector import (
    build_digest_for_checkpoint,
    create_git_aware_task_services,
)
from project_evolution.mcp.errors import INVALID_ARGUMENT, ToolError
from project_evolution.mcp.registry import ProjectRegistry
from project_evolution.mcp.service import PelToolService
from project_evolution.models import RawEvent

PRJ = "PRJ-test"


def init_repo_at(path: Path) -> Path:
    """在指定位置（而非 tmp_path/repo）初始化隔离配置的 Git 仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q")
    return path


def file_events(services, checkpoint_id: str | None = None) -> list[RawEvent]:
    return [
        event
        for event in services.store.replay()
        if event.kind in ("file_added", "file_modified", "file_deleted")
        and (checkpoint_id is None or event.checkpoint_id == checkpoint_id)
    ]


def commit_events(services, checkpoint_id: str | None = None) -> list[RawEvent]:
    return [
        event
        for event in services.store.replay()
        if event.kind == "git_commit"
        and (checkpoint_id is None or event.checkpoint_id == checkpoint_id)
    ]


# --------------------------------------------- committed 变更可见（06 §4/§9）


def test_committed_change_between_checkpoints_is_visible(tmp_path: Path) -> None:
    """start（干净）→ 修改并 commit → checkpoint：变更必须被采集（曾整体漏掉）。"""
    repo = init_repo(tmp_path)
    (repo / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    commit_all(repo, "init")

    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "乘法功能")

    (repo / "calc.py").write_bytes(
        b"def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    commit_all(repo, "feat: add mul")  # 工作区回到干净

    cp = services.checkpoints.create_checkpoint(task.id, summary="mul 完成")
    events = file_events(services, cp.id)
    assert [e.payload["path"] for e in events] == ["calc.py"]
    assert events[0].kind == "file_modified"

    # diff evidence 落盘且包含 commit 进去的 diff 内容
    cp_created = next(
        e
        for e in services.store.replay()
        if e.kind == "checkpoint_created" and e.checkpoint_id == cp.id
    )
    evidence_id = cp_created.payload["diff_evidence_id"]
    patch = (
        repo / ".project-evolution" / "evidence" / "diffs" / f"{evidence_id}.patch"
    ).read_text(encoding="utf-8")
    assert "+def mul" in patch

    # digest（模型输入）同样包含 committed 变更
    digest = build_digest_for_checkpoint(
        repo, project_id=PRJ, task_id=task.id, checkpoint_id=cp.id
    )
    assert digest.changed_files == ["calc.py"]
    assert digest.scope_confidence == "normal"


def test_committed_and_uncommitted_changes_both_captured(tmp_path: Path) -> None:
    """commit 一个文件 + 另一个文件保持脏：两类变更都进入增量。"""
    repo = init_repo(tmp_path)
    (repo / "a.py").write_bytes(b"a\n")
    (repo / "b.py").write_bytes(b"b\n")
    commit_all(repo, "init")

    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "a.py").write_bytes(b"a committed\n")
    commit_all(repo, "edit a")
    (repo / "b.py").write_bytes(b"b dirty\n")

    cp = services.checkpoints.create_checkpoint(task.id)
    assert sorted(e.payload["path"] for e in file_events(services, cp.id)) == [
        "a.py",
        "b.py",
    ]


def test_git_commit_events_attributed_to_checkpoint(tmp_path: Path) -> None:
    """快照 HEAD 前进时产生归属当前 Task/Checkpoint 的 git_commit RawEvents（06 §9）。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")

    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (repo / "app.py").write_bytes(b"v2\n")
    sha1 = commit_all(repo, "second commit")
    (repo / "app.py").write_bytes(b"v3\n")
    sha2 = commit_all(repo, "third commit")

    cp = services.checkpoints.create_checkpoint(task.id)
    commits = commit_events(services, cp.id)
    assert [e.payload["sha"] for e in commits] == [sha1, sha2]
    assert commits[0].payload["subject"] == "second commit"
    assert commits[0].task_id == task.id
    assert commits[0].checkpoint_id == cp.id
    assert commits[0].payload["evidence_id"].startswith("EVD-")

    # HEAD 未前进的 checkpoint（只有脏变更）不产生 git_commit 事件
    (repo / "app.py").write_bytes(b"v4 dirty\n")
    cp2 = services.checkpoints.create_checkpoint(task.id)
    assert commit_events(services, cp2.id) == []


def test_committed_change_visible_in_mcp_checkpoint_response(tmp_path: Path) -> None:
    """MCP checkpoint_task 响应的 changed_files 包含 committed 变更。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")
    service = PelToolService(ProjectRegistry.single(repo, name="demo"))
    started = service.start_task({"title": "t"})
    (repo / "app.py").write_bytes(b"v2\n")
    commit_all(repo, "edit")
    result = service.checkpoint_task({"task_id": started["task_id"], "summary": "cp"})
    assert result["evidence"]["changed_files"] == ["app.py"]
    service.shutdown()


# ------------------------------------------------------ MCP get_history file


def test_get_history_file_kind_returns_file_history(tmp_path: Path) -> None:
    """kind=file 必须走 File History（曾误入 bug 分支恒返回空）。"""
    repo = init_repo(tmp_path)
    (repo / "app.py").write_bytes(b"v1\n")
    commit_all(repo, "init")
    service = PelToolService(ProjectRegistry.single(repo, name="demo"))
    started = service.start_task({"title": "t"})
    (repo / "app.py").write_bytes(b"v2\n")
    service.checkpoint_task({"task_id": started["task_id"], "summary": "cp"})

    result = service.get_history({"kind": "file", "query": "app.py"})
    history = result["results"][0]
    assert history["path"] == "app.py"
    assert [c["path"] for c in history["file_changes"]] == ["app.py"]
    assert history["file_changes"][0]["event_id"].startswith("RAW-")
    assert history["tasks"][0]["task_id"] == started["task_id"]

    with pytest.raises(ToolError) as exc_info:
        service.get_history({"kind": "file"})
    assert exc_info.value.code == INVALID_ARGUMENT
    service.shutdown()


# ------------------------------------------------------- 嵌套仓库 scope 采集


def test_adapter_detect_scope(tmp_path: Path) -> None:
    outer = init_repo(tmp_path)
    sub = outer / "sub"
    sub.mkdir()
    assert GitAdapter.detect(sub).scope == "sub"
    assert GitAdapter.detect(outer).scope == ""
    deep = sub / "deep"
    deep.mkdir()
    assert GitAdapter.detect(deep).scope == "sub/deep"


def test_nested_project_collects_only_subtree(tmp_path: Path) -> None:
    """项目目录是外层仓库子目录：只采集子树，父仓库变更绝不混入。"""
    outer = init_repo_at(tmp_path / "outer")
    (outer / "root.py").write_bytes(b"root\n")
    sub = outer / "sub"
    sub.mkdir()
    (sub / "app.py").write_bytes(b"v1\n")
    commit_all(outer, "init")

    services = create_git_aware_task_services(sub, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "sub project task")

    # 父仓库根上的脏变更：不属于本项目
    (outer / "root.py").write_bytes(b"root dirty\n")
    # 子树内：脏变更 + commit 各一个
    (sub / "app.py").write_bytes(b"v2 dirty\n")
    (sub / "committed.py").write_bytes(b"c1\n")
    commit_all(outer, "touch sub")

    cp = services.checkpoints.create_checkpoint(task.id)
    paths = sorted(e.payload["path"] for e in file_events(services, cp.id))
    assert paths == ["sub/app.py", "sub/committed.py"]  # root.py 不在其中

    # 子树内 PEL 目录（sub/.project-evolution）同样被排除
    for event in file_events(services):
        assert ".project-evolution" not in event.payload["path"]

    # 只动父仓库根、不动子树的 commit 区间不产生任何子树事件
    (outer / "root.py").write_bytes(b"root dirty2\n")
    commit_all(outer, "outer only")
    (sub / "root-untouched.marker").unlink(missing_ok=True)
    cp2 = services.checkpoints.create_checkpoint(task.id)
    assert file_events(services, cp2.id) == []
    assert commit_events(services, cp2.id) == []  # outer-only commit 不归属本项目

    # git_commit 事件只包含触及子树的提交
    commits = commit_events(services, cp.id)
    assert [e.payload["subject"] for e in commits] == ["touch sub"]


def test_nested_project_snapshots_scoped(tmp_path: Path) -> None:
    """嵌套项目的快照 files 键只含子树路径（带 sub/ 前缀）。"""
    outer = init_repo_at(tmp_path / "outer")
    (outer / "root.py").write_bytes(b"root\n")
    sub = outer / "sub"
    sub.mkdir()
    (sub / "app.py").write_bytes(b"v1\n")
    commit_all(outer, "init")

    services = create_git_aware_task_services(sub, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "t")
    (outer / "root.py").write_bytes(b"root dirty\n")  # 父仓库脏文件
    (sub / "app.py").write_bytes(b"v2\n")
    services.checkpoints.create_checkpoint(task.id)

    snapshots = sorted(
        (sub / ".project-evolution" / "evidence" / "snapshots").rglob("*.json")
    )
    assert snapshots
    for snap_file in snapshots:
        data = json.loads(snap_file.read_text(encoding="utf-8"))
        for path in data["files"]:
            assert path.startswith("sub/"), path
            assert ".project-evolution" not in path


# ------------------------------------------------------------- 次要修复


def test_project_display_name_resolves_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_evolution.dashboard.report import project_display_name

    project = tmp_path / "my-project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert project_display_name(".") == "my-project"


def test_dashboard_project_name_and_nosniff_with_relative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fastapi_testclient = pytest.importorskip(
        "fastapi.testclient", reason="dashboard extra"
    )
    from project_evolution.dashboard import create_app

    project = tmp_path / "my-project"
    project.mkdir()
    (project / ".project-evolution").mkdir()
    monkeypatch.chdir(project)
    with fastapi_testclient.TestClient(create_app(".")) as client:
        overview = client.get("/api/overview").json()
        assert overview["project_name"] == "my-project"
        assert overview["project_root"].endswith("my-project")
        # nosniff：JSON 中的用户文本永不被浏览器嗅探为 HTML
        assert (
            client.get("/api/overview").headers["X-Content-Type-Options"] == "nosniff"
        )
        assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"


def test_api_unknown_entities_return_404(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip(
        "fastapi.testclient", reason="dashboard extra"
    )
    from project_evolution.dashboard import create_app

    with fastapi_testclient.TestClient(create_app(tmp_path)) as client:
        for path in (
            "/api/parameters/NoSuchParam",
            "/api/features/NoSuchFeature",
            "/api/decisions/NoSuchDecision",
        ):
            assert client.get(path).status_code == 404, path
