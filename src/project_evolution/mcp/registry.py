"""Project Registry / Resolution（phase-07 §4，08-mcp-integration.md §3）。

一个 MCP Server 可以服务多个项目；Project 选择必须显式，解析优先级：

1. 工具参数中的 ``project``（按 name / id / root 路径精确匹配，命中必须唯一）；
2. 当前已绑定 project（本 Server 连接内上一次成功解析的结果）；
3. 单项目 pinned（registry 只有一个条目）；
4. 否则报错，禁止猜测（phase-07 §4：无法唯一解析时必须报错，不自动猜错项目）。

Registry 文件格式（UTF-8 JSON）::

    {"projects": [{"name": "screepin", "id": "PRJ-0001", "root": "D:/code/ScreenPin"}]}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from project_evolution.mcp.errors import PROJECT_AMBIGUOUS, PROJECT_NOT_FOUND, ToolError

#: registry 文件本身非法（结构/条目/重复 name）；加载期致命错误
INVALID_REGISTRY = "INVALID_REGISTRY"


class ProjectEntry(BaseModel):
    """一个可服务的项目：name 唯一、root 为项目根目录。"""

    name: str
    root: str
    id: str | None = None

    def matches(self, ref: str) -> bool:
        """显式 ``project`` 参数是否指向本条目（name / id / root 路径）。"""
        if ref == self.name or (self.id is not None and ref == self.id):
            return True
        try:
            return _same_path(self.root, ref)
        except (OSError, ValueError):
            return False


def _same_path(left: str, right: str) -> bool:
    return Path(os.path.normcase(os.path.abspath(left))) == Path(
        os.path.normcase(os.path.abspath(right))
    )


class ProjectRegistry:
    """条目集合 + 解析规则（规则见模块 docstring）。"""

    def __init__(self, entries: list[ProjectEntry]):
        self._entries = list(entries)

    # ------------------------------------------------------------------ load

    @property
    def entries(self) -> list[ProjectEntry]:
        return list(self._entries)

    @classmethod
    def single(cls, root: str | Path, *, name: str | None = None) -> ProjectRegistry:
        """单项目 pinned server（phase-07 §4）。"""
        root_path = Path(root).resolve()
        return cls(
            [
                ProjectEntry(
                    name=name or root_path.name or "default",
                    root=str(root_path),
                )
            ]
        )

    @classmethod
    def load(cls, registry_file: str | Path) -> ProjectRegistry:
        """从 JSON registry 文件加载（多项目）。

        :raises ToolError: 文件不存在 / 不是合法 JSON / 结构或条目非法 /
            name 重复（重复 name 会让显式解析产生歧义，加载期即拒绝）。
        """
        path = Path(registry_file)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ToolError(
                PROJECT_NOT_FOUND, f"cannot read project registry {path}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ToolError(
                INVALID_REGISTRY, f"invalid JSON in project registry {path}: {exc}"
            ) from exc
        entries_raw = raw.get("projects") if isinstance(raw, dict) else None
        if not isinstance(entries_raw, list) or not entries_raw:
            raise ToolError(
                INVALID_REGISTRY,
                f"project registry {path} must contain a non-empty 'projects' list",
            )
        entries: list[ProjectEntry] = []
        for index, item in enumerate(entries_raw):
            if not isinstance(item, dict):
                raise ToolError(
                    INVALID_REGISTRY, f"registry entry #{index} must be a mapping"
                )
            name = item.get("name")
            root = item.get("root")
            if not isinstance(name, str) or not name.strip():
                raise ToolError(
                    INVALID_REGISTRY, f"registry entry #{index} needs a non-empty 'name'"
                )
            if not isinstance(root, str) or not root.strip():
                raise ToolError(
                    INVALID_REGISTRY, f"registry entry #{index} needs a non-empty 'root'"
                )
            project_id = item.get("id")
            if project_id is not None and not isinstance(project_id, str):
                raise ToolError(
                    INVALID_REGISTRY, f"registry entry #{index} 'id' must be a string"
                )
            entries.append(ProjectEntry(name=name, root=root, id=project_id))
        names = [entry.name for entry in entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ToolError(
                PROJECT_AMBIGUOUS,
                f"duplicate project name(s) in registry {path}: {duplicates}",
            )
        return cls(entries)

    # --------------------------------------------------------------- resolve

    def resolve(
        self,
        project: str | None = None,
        *,
        current: ProjectEntry | None = None,
    ) -> ProjectEntry:
        """按 08 §3 优先级解析项目；无法唯一解析时抛 ToolError。

        :param project: 工具参数中的显式项目引用（name / id / root）。
        :param current: 本连接内上一次绑定的条目（优先级 2）。
        """
        if project is not None and str(project).strip():
            ref = str(project).strip()
            hits = [entry for entry in self._entries if entry.matches(ref)]
            if not hits:
                known = ", ".join(entry.name for entry in self._entries)
                raise ToolError(
                    PROJECT_NOT_FOUND,
                    f"project {ref!r} is not served by this server (known: {known})",
                )
            if len(hits) > 1:
                names = ", ".join(entry.name for entry in hits)
                raise ToolError(
                    PROJECT_AMBIGUOUS,
                    f"project {ref!r} matches multiple entries: {names}; "
                    "pass the exact name or root",
                )
            return hits[0]
        if current is not None:
            return current
        if len(self._entries) == 1:
            return self._entries[0]
        if not self._entries:
            raise ToolError(PROJECT_NOT_FOUND, "no project is configured for this server")
        names = ", ".join(entry.name for entry in self._entries)
        raise ToolError(
            PROJECT_AMBIGUOUS,
            f"multiple projects are served ({names}); pass 'project' explicitly",
        )

    def validate_roots(self) -> None:
        """启动期校验：每个条目的 root 必须是存在的目录（失败即拒绝启动）。"""
        for entry in self._entries:
            if not Path(entry.root).is_dir():
                raise ToolError(
                    PROJECT_NOT_FOUND,
                    f"project root of {entry.name!r} does not exist: {entry.root}",
                )


def entry_payload(entry: ProjectEntry, *, project_id: str | None = None) -> dict[str, Any]:
    """工具响应中的 project 标识（紧凑、稳定）。"""
    payload: dict[str, Any] = {"name": entry.name, "root": entry.root}
    if project_id:
        payload["id"] = project_id
    return payload
