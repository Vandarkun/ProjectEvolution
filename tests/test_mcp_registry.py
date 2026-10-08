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


def test_single_registry_resolves_relative_root(tmp_path: Path, monkeypatch) -> None:
    """冒烟回归：``--project .`` 这类相对路径不得产生空项目名或相对 root。"""
    monkeypatch.chdir(tmp_path)
    registry = ProjectRegistry.single(".")
    entry = registry.entries[0]
    assert entry.name == tmp_path.name  # 非空、来自解析后的目录名
    assert Path(entry.root).is_absolute()
    assert Path(entry.root) == tmp_path.resolve()


def test_load_registry_resolves_relative_roots_against_file_location(
    tmp_path: Path, monkeypatch
) -> None:
    """registry 文件里的相对 root 按 registry 文件所在目录解析，与 cwd 无关。"""
    (tmp_path / "projA").mkdir()
    path = write_registry(tmp_path, {"projects": [{"name": "a", "root": "projA"}]})
    monkeypatch.chdir(tmp_path)  # 从任意 cwd 加载
    registry = ProjectRegistry.load(path)
    entry = registry.entries[0]
    assert Path(entry.root).is_absolute()
    assert Path(entry.root) == (tmp_path / "projA").resolve()

    # cwd 漂移后：validate / matches 仍指向 registry 旁边的 projA，不跟 cwd 走
    monkeypatch.chdir(tmp_path.parent)
    registry.validate_roots()
    assert registry.resolve("a").root == str((tmp_path / "projA").resolve())
    assert registry.resolve(str(tmp_path / "projA")).name == "a"


def test_root_matching_tolerates_short_path_and_case(tmp_path: Path) -> None:
    """路径等价：8.3 短名 / 大小写 / 正反斜杠拼法不同仍命中同一项目。"""
    import sys

    repo = init_repo(tmp_path)
    registry = ProjectRegistry.single(repo)
    lowered = str(repo).lower()
    backslashed = lowered.replace("/", "\\") if "/" in lowered else lowered
    assert registry.resolve(backslashed).root == str(Path(repo).resolve())
    if sys.platform == "win32":
        short = _to_short_path(str(repo))
        if short != str(repo):  # 系统禁用 8.3 时跳过该断言
            assert registry.resolve(short).root == str(Path(repo).resolve())


def _to_short_path(path: str) -> str:
    """尽量把路径转成 8.3 短名形式（不可用时原样返回；仅 Windows 可调用）。"""
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 260)  # type: ignore[attr-defined]
    return buffer.value if length else path


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
