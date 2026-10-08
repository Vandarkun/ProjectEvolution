"""共享测试 fixture。

``query_service``：种子事件流（Task-1 透明度 / Task-2 Hook）+ 全量重建投影。
种子构造函数保持在 test_query_service.py（本 fixture 惰性导入，避免
conftest 提前拉起测试模块）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_evolution.index import QueryService, rebuild_projection


@pytest.fixture()
def query_service(tmp_path: Path) -> QueryService:
    from test_query_service import seed_event_stream

    project = tmp_path / "project"
    project.mkdir()
    seed_event_stream(project)
    rebuild_projection(project)
    return QueryService(project)
