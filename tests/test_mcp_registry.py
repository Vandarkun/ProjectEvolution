"""Project Registry / Resolution 测试（phase-07 §4，08 §3）。

解析优先级与拒绝规则：

1. 显式 ``project``（name / id / root）精确匹配，歧义即报错；
2. 当前已绑定 project；
3. 单项目 pinned；
4. 否则 PROJECT_AMBIGUOUS / PROJECT_NOT_FOUND——绝不静默猜测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo

from project_evolution.mcp.errors import (
    PROJECT_AMBIGUOUS,
    PROJECT_NOT_FOUND,
    ToolError,
)
from project_evolution.mcp.registry import (
    INVALID_REGISTRY,
    ProjectEntry,
    ProjectRegistry,
)


def test_single_pinned_resolves_without_project_param(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    registry = ProjectRegistry.single(repo, name="screepin")
    entry = registry.resolve(None)
    assert entry.name == "screepin"
    assert entry.root == str(repo)


def test_explicit_match_by_name_id_and_root(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("a")
    commit_all(repo, "init")
    other = tmp_path / "other"
    other.mkdir()
    registry = ProjectRegistry(
        [
            ProjectEntry(name="alpha", root=str(repo), id="PRJ-1"),
            ProjectEntry(name="beta", root=str(other)),
        ]
    )
    assert registry.resolve("alpha").id == "PRJ-1"
    assert registry.resolve("PRJ-1").name == "alpha"
    assert registry.resolve(str(repo)).name == "alpha"
    assert registry.resolve(str(other)).name == "beta"


def test_explicit_unknown_project_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    registry = ProjectRegistry.single(repo)
    with pytest.raises(ToolError) as excinfo:
        registry.resolve("nonexistent")
    assert excinfo.value.code == PROJECT_NOT_FOUND


def test_multi_project_without_hint_is_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = init_repo(tmp_path / "a")
    repo_b = init_repo(tmp_path / "b")
    registry = ProjectRegistry(
        [ProjectEntry(name="a", root=str(repo_a)), ProjectEntry(name="b", root=str(repo_b))]
    )
    with pytest.raises(ToolError) as excinfo:
        registry.resolve(None)
    assert excinfo.value.code == PROJECT_AMBIGUOUS


def test_current_binding_takes_priority_over_pin(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = init_repo(tmp_path / "a")
    repo_b = init_repo(tmp_path / "b")
    registry = ProjectRegistry(
        [ProjectEntry(name="a", root=str(repo_a)), ProjectEntry(name="b", root=str(repo_b))]
    )
    bound = registry.resolve("b")
    # 已绑定后，无参调用继续解析到同一项目（08 §3 优先级 2）
    assert registry.resolve(None, current=bound).name == "b"


def test_name_colliding_with_other_id_is_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = init_repo(tmp_path / "a")
    repo_b = init_repo(tmp_path / "b")
    registry = ProjectRegistry(
        [
            ProjectEntry(name="alpha", root=str(repo_a), id="PRJ-1"),
            # "alpha" 既是前者 name 又是后者的 root 目录名——显式 root 匹配不受影响，
            # 这里构造 name 与 id 的冲突：beta.id == alpha.name
            ProjectEntry(name="beta", root=str(repo_b), id="alpha"),
        ]
    )
    with pytest.raises(ToolError) as excinfo:
        registry.resolve("alpha")
    assert excinfo.value.code == PROJECT_AMBIGUOUS


# ------------------------------------------------------------------- loading


def write_registry(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_registry_file(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    path = write_registry(
        tmp_path, {"projects": [{"name": "solo", "root": str(repo), "id": "PRJ-9"}]}
    )
    registry = ProjectRegistry.load(path)
    assert [entry.name for entry in registry.entries] == ["solo"]
    registry.validate_roots()


def test_load_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    path = write_registry(
        tmp_path,
        {
            "projects": [
                {"name": "same", "root": str(repo)},
                {"name": "same", "root": str(tmp_path)},
            ]
        },
    )
    with pytest.raises(ToolError) as excinfo:
        ProjectRegistry.load(path)
    assert excinfo.value.code in (INVALID_REGISTRY, PROJECT_AMBIGUOUS)


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 缺 projects
        {"projects": []},  # 空列表
        {"projects": [{"root": "x"}]},  # 缺 name
        {"projects": [{"name": "x"}]},  # 缺 root
    ],
)
def test_load_registry_rejects_bad_structure(tmp_path: Path, payload: dict) -> None:
    path = write_registry(tmp_path, payload)
    with pytest.raises(ToolError) as excinfo:
        ProjectRegistry.load(path)
    assert excinfo.value.code == INVALID_REGISTRY


def test_load_registry_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        ProjectRegistry.load(tmp_path / "nope.json")
    assert excinfo.value.code == PROJECT_NOT_FOUND


def test_validate_roots_rejects_missing_directory(tmp_path: Path) -> None:
    registry = ProjectRegistry([ProjectEntry(name="ghost", root=str(tmp_path / "ghost"))])
    with pytest.raises(ToolError) as excinfo:
        registry.validate_roots()
    assert excinfo.value.code == PROJECT_NOT_FOUND
