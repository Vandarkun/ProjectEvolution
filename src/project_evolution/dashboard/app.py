"""FastAPI read-only API + 页面路由（phase-08 §3，11 §4）。

- 全部路由都是 GET：Dashboard 只读，不提供任何写入口；
- 数据只来自 Query Service（先做增量投影追平，失败标记 stale 但不阻塞，
  与 MCP 查询路径一致）；页面由 :mod:`pages` 服务端渲染；
- FastAPI / uvicorn 属于可选 extra（``pip install project-evolution[dashboard]``），
  缺失时给出明确错误提示，不影响 Core / MCP / CLI 其余命令。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from project_evolution.config import PelConfig, load_config
from project_evolution.dashboard import pages
from project_evolution.dashboard.report import build_overview, load_evidence_content
from project_evolution.errors import PelError
from project_evolution.index import (
    TIMELINE_KINDS,
    ProjectionNotBuiltError,
    QueryService,
    update_projection,
)

#: Timeline 默认返回条数（页面/API 相同；?limit= 可调，上限同 MCP）
DEFAULT_TIMELINE_LIMIT = 200
MAX_TIMELINE_LIMIT = 1000

_HELP_EXTRA = (
    "dashboard requires the optional web stack; "
    "install it with: pip install 'project-evolution[dashboard]'"
)


def _require_fastapi() -> Any:
    try:
        import fastapi
    except ImportError as exc:  # pragma: no cover - 环境缺 extra 时
        raise RuntimeError(_HELP_EXTRA) from exc
    return fastapi


def create_app(project_root: str | Path, *, config: PelConfig | None = None) -> Any:
    """构建只读 Dashboard 应用（FastAPI 实例）。"""
    fastapi = _require_fastapi()
    from fastapi import HTTPException, Query
    from fastapi.responses import HTMLResponse

    root = Path(project_root)
    resolved_config = config if config is not None else load_config(root)
    service = QueryService(root, config=resolved_config)

    def catch_up() -> None:
        """查询前增量追平投影；失败不阻塞查询（stale 标记已由投影层维护）。"""
        with contextlib.suppress(PelError):
            update_projection(root, config=resolved_config)

    def query() -> QueryService:
        catch_up()
        return service

    app = fastapi.FastAPI(
        title="PEL Dashboard",
        version="0.1.0",
        description="Read-only dashboard over the Project Evolution Query Service.",
    )

    # ------------------------------------------------------------- json api

    @app.get("/api/overview")
    def api_overview() -> Any:
        return build_overview(query()).model_dump(mode="json")

    @app.get("/api/timeline")
    def api_timeline(
        kinds: str | None = Query(default=None, description="逗号分隔 kind"),  # noqa: B008
        limit: int = Query(  # noqa: B008
            default=DEFAULT_TIMELINE_LIMIT, ge=1, le=MAX_TIMELINE_LIMIT
        ),
    ) -> Any:
        kind_list = None if not kinds else [k for k in kinds.split(",") if k.strip()]
        try:
            items = query().get_timeline(kind_list, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "count": len(items),
            "items": [item.model_dump(mode="json") for item in items],
        }

    @app.get("/api/tasks")
    def api_tasks() -> Any:
        tasks = query().list_tasks()
        return {"count": len(tasks), "items": [t.model_dump(mode="json") for t in tasks]}

    @app.get("/api/tasks/{task_id}")
    def api_task(task_id: str) -> Any:
        history = query().get_task_history(task_id)
        if history is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return history.model_dump(mode="json")

    @app.get("/api/parameters/{name}")
    def api_parameter(
        name: str, scope: str | None = Query(default=None)  # noqa: B008
    ) -> Any:
        return query().get_parameter_history(name, scope=scope).model_dump(mode="json")

    @app.get("/api/features/{name}")
    def api_feature(name: str) -> Any:
        return query().get_feature_history(name).model_dump(mode="json")

    @app.get("/api/decisions/{subject}")
    def api_decision(subject: str) -> Any:
        return query().get_decision_history(subject).model_dump(mode="json")

    @app.get("/api/bugs")
    def api_bugs(scope: str | None = Query(default=None)) -> Any:  # noqa: B008
        bugs = query().get_bug_history(scope=scope)
        return {"count": len(bugs), "items": [bug.model_dump(mode="json") for bug in bugs]}

    @app.get("/api/evidence/{evidence_id}")
    def api_evidence(evidence_id: str) -> Any:
        record, content, truncated = load_evidence_content(query(), evidence_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"evidence not found: {evidence_id}")
        payload: dict[str, Any] = record.model_dump(mode="json")
        payload["content"] = content
        payload["truncated"] = truncated
        return payload

    # --------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    def page_overview() -> Any:
        return pages.overview_page(build_overview(query()))

    @app.get("/timeline", response_class=HTMLResponse)
    def page_timeline(
        kind: list[str] | None = Query(default=None),  # noqa: B008
        limit: int = Query(  # noqa: B008
            default=DEFAULT_TIMELINE_LIMIT, ge=1, le=MAX_TIMELINE_LIMIT
        ),
    ) -> Any:
        selected = list(kind or [])
        try:
            items = query().get_timeline(selected or None, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return pages.timeline_page(items, selected or list(TIMELINE_KINDS))

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def page_task(task_id: str) -> Any:
        history = query().get_task_history(task_id)
        if history is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return pages.task_detail_page(history)

    @app.get("/parameters", response_class=HTMLResponse)
    def page_parameter(
        name: str = Query(default=""),  # noqa: B008
        scope: str | None = Query(default=None),  # noqa: B008
    ) -> Any:
        if not name:
            return page_overview_redirect()
        history = query().get_parameter_history(name, scope=scope)
        if not history.changes:
            raise HTTPException(status_code=404, detail=f"parameter not found: {name}")
        return pages.parameter_page(history)

    @app.get("/evidence/{evidence_id}", response_class=HTMLResponse)
    def page_evidence(evidence_id: str) -> Any:
        record, content, truncated = load_evidence_content(query(), evidence_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"evidence not found: {evidence_id}")
        return pages.evidence_page(record, content, truncated=truncated)

    def page_overview_redirect() -> Any:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/", status_code=303)

    @app.exception_handler(ProjectionNotBuiltError)
    def projection_not_built(_request: Any, _exc: ProjectionNotBuiltError) -> Any:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            "projection not built yet; run `pel index rebuild` first", status_code=503
        )

    app.state.project_root = str(root)
    return app


def run_dashboard(
    project_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8642,
) -> None:
    """启动本地 Dashboard；默认只监听 loopback（11 §9）。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - 环境缺 extra 时
        raise RuntimeError(_HELP_EXTRA) from exc
    uvicorn.run(create_app(project_root), host=host, port=port, log_level="warning")
