"""Tool Service：MCP 五个工具 + 可选 get_evidence 在 Core API 上的薄层。

MCP 层只做（08-mcp-integration.md §1）：参数解析、Project 解析、调用 Core
Service、返回结构化结果。不做 Git 业务逻辑 / Evolution Prompt / Ledger
直接操作 / Dashboard 逻辑。

关键语义：

- 语义分析默认异步（phase-07 §6）：checkpoint 事实与 Evidence 保存成功即
  返回，``analysis_status=pending``，语义结果由后台 worker 补齐；
- partial success：Checkpoint 保存成功与 Evolution Model 失败是两件事，
  后者绝不把整个工具调用判为 failed；
- 查询前先做增量投影追平（update_projection），投影元数据随响应返回，
  明确区分 in-sync / stale。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.errors import PelError, StorageError, ValidationError
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.index import QueryService, update_projection
from project_evolution.index.query import ProjectionStatus
from project_evolution.mcp.errors import (
    EVIDENCE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    LEDGER_WRITE_FAILED,
    TASK_NOT_FOUND,
    ToolError,
    tool_error_from_exception,
)
from project_evolution.mcp.registry import ProjectEntry, ProjectRegistry, entry_payload
from project_evolution.models import RawEvent

_LOG = logging.getLogger(__name__)

#: 工具响应中 diff patch 内容的裁剪上限（08 §9：大 Patch 不塞满 Agent Context）
MAX_EVIDENCE_CHARS = 20_000

#: get_history 各列表的默认 / 最大条数
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 200

AnalysisStatus = str  # pending | committed | noop | disabled | none

_HISTORY_KINDS = ("task", "parameter", "feature", "decision", "bug", "file", "version")


class ProviderSpec:
    """Provider 覆盖参数（全部可空：缺省回退项目 config.yaml，再缺省 mock）。"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env


def _env_get(name: str) -> str | None:
    import os

    return os.environ.get(name)


def build_provider(spec: ProviderSpec, config: PelConfig):
    """按 spec + 项目配置构造 Provider；未配置 model 的真实 Provider 明确报错。"""
    from project_evolution.evolution.providers import (
        MockProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
    )

    model = spec.model or config.analyzer_model
    provider = spec.provider or config.analyzer_provider or "mock"
    if provider == "mock":
        return MockProvider()
    if provider == "openai":
        if not model:
            raise ToolError(
                INVALID_ARGUMENT,
                "openai provider requires --model or config analyzer_model",
            )
        env_name = spec.api_key_env or config.analyzer_api_key_env
        api_key = config.analyzer_api_key_resolved
        if env_name and not config.analyzer_api_key:
            api_key = api_key or _env_get(env_name)
        return OpenAICompatibleProvider(
            model=model,
            base_url=spec.base_url or config.analyzer_base_url or "https://api.openai.com/v1",
            api_key=api_key,
        )
    if provider == "ollama":
        if not model:
            raise ToolError(
                INVALID_ARGUMENT,
                "ollama provider requires --model or config analyzer_model",
            )
        return OllamaProvider(
            model=model, base_url=spec.base_url or "http://localhost:11434"
        )
    raise ToolError(INVALID_ARGUMENT, f"unknown provider: {provider!r}")


class ProjectRuntime:
    """单个项目的组合根缓存：Core services + 推断的 project_id + QueryService。"""

    def __init__(
        self,
        entry: ProjectEntry,
        *,
        provider_factory: Callable[[PelConfig], object] | None,
    ):
        self.entry = entry
        self.root = Path(entry.root)
        self.config = load_config(self.root)
        ledger_id = self._ledger_project_id()
        #: 账本已有 project_id 时沿用（Canonical 一致性）；否则按根目录名生成
        #: 稳定 id，并在首次 start_task 写入账本后固定下来。
        slug = "".join(ch if ch.isalnum() else "-" for ch in self.root.name.lower())
        self.project_id: str = ledger_id or f"PRJ-{slug.strip('-') or 'default'}"
        self.inferred_id = ledger_id is None
        provider = provider_factory(self.config) if provider_factory is not None else None
        self.services = create_git_aware_task_services(
            self.root, project_id=self.project_id, analysis_provider=provider
        )
        self.query = QueryService(self.root, config=self.config)

    def _ledger_project_id(self) -> str | None:
        """从账本首个事件读取 project_id（Canonical 一致性优先）。"""
        path = self.root / PEL_DIR_NAME / self.config.events_dir / "raw.jsonl"
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        value = record.get("project_id")
                        return value if isinstance(value, str) else None
                    except ValueError:
                        continue
        except OSError:
            return None
        return None

    def _infer_project_id(self) -> str | None:
        """从账本首个事件读取 project_id（Canonical 一致性优先）。"""
        path = self.root / PEL_DIR_NAME / self.config.events_dir / "raw.jsonl"
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        value = record.get("project_id")
                        return value if isinstance(value, str) else None
                    except ValueError:
                        continue
        except OSError:
            return None
        return None

    @property
    def worker(self):
        return getattr(self.services, "analysis_worker", None)

    def analysis_status(self) -> AnalysisStatus:
        return "pending" if self.worker is not None else "disabled"

    def final_analysis_status(self, checkpoint_id: str) -> AnalysisStatus:
        """drain 之后该 checkpoint 的最终分析状态（CLI 同步模式输出用）。"""
        worker = self.worker
        if worker is None:
            return "disabled"
        for _task_id, cp_id, status in worker.completed:
            if cp_id == checkpoint_id:
                return status  # committed / noop
        # 未完成：要么失败转 pending（worker.failures），要么仍在队列
        return "pending"

    def stop(self, *, timeout: float) -> None:
        worker = self.worker
        if worker is not None:
            worker.stop(drain=True, timeout=timeout)


class PelToolService:
    """MCP / CLI 共用的工具实现（不含任何传输层细节）。"""

    def __init__(
        self,
        registry: ProjectRegistry,
        *,
        provider_factory: Callable[[PelConfig], object] | None = None,
        source: str = "mcp",
    ):
        self._registry = registry
        self._provider_factory = provider_factory
        self._source = source
        self._runtimes: dict[str, ProjectRuntime] = {}
        self._current: ProjectEntry | None = None

    # ------------------------------------------------------------- lifecycle

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """停止全部后台 worker（尽力 drain；失败分析已在 pending 队列）。"""
        for runtime in self._runtimes.values():
            runtime.stop(timeout=timeout)
        self._runtimes.clear()

    def drain_analysis(self, *, timeout: float = 300.0) -> bool:
        """等待全部后台分析完成（CLI 同步模式）；超时返回 False。"""
        drained = True
        for runtime in self._runtimes.values():
            worker = runtime.worker
            if worker is not None and not worker.wait_idle(timeout=timeout):
                drained = False
        return drained

    def resolve_analysis_status(self, checkpoint_id: str) -> AnalysisStatus:
        """查某个 checkpoint 的最终分析状态（需先 drain_analysis）。"""
        for runtime in self._runtimes.values():
            return runtime.final_analysis_status(checkpoint_id)
        return "none"

    def _resolve(self, project: Any) -> ProjectRuntime:
        if project is not None and not isinstance(project, str):
            raise ToolError(INVALID_ARGUMENT, "'project' must be a string")
        entry = self._registry.resolve(project, current=self._current)
        runtime = self._runtime_for(entry)
        self._current = entry  # 绑定本连接的当前项目（08 §3 优先级 2）
        return runtime

    def _runtime_for(self, entry: ProjectEntry) -> ProjectRuntime:
        key = str(Path(entry.root).resolve())
        if key not in self._runtimes:
            self._runtimes[key] = ProjectRuntime(
                entry, provider_factory=self._provider_factory
            )
        return self._runtimes[key]

    def _project_block(self, runtime: ProjectRuntime) -> dict[str, Any]:
        return entry_payload(runtime.entry, project_id=runtime.project_id)

    # ----------------------------------------------------------------- tools

    def start_task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ToolError(INVALID_ARGUMENT, "'title' must be a non-empty string")
        agent = _optional_str(arguments, "agent")
        session_id = _optional_str(arguments, "session_id")
        runtime = self._resolve(arguments.get("project"))
        try:
            task = runtime.services.tasks.start_task(
                runtime.project_id,
                title,
                agent=agent,
                session_id=session_id,
                source=self._source,
            )
        except PelError as exc:
            raise tool_error_from_exception(exc) from exc
        started = _find_event(runtime, lambda e: e.kind == "task_started" and e.task_id == task.id)
        return {
            "project": self._project_block(runtime),
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "baseline": task.baseline_ref,
            "baseline_degraded": bool(
                started is not None and started.payload.get("baseline_degraded")
            ),
            "session_id": session_id or (started.session_id if started else None),
        }

    def checkpoint_task(self, arguments: Mapping[str, Any], **_extra: Any) -> dict[str, Any]:
        task_id = _required_str(arguments, "task_id")
        summary = _optional_str(arguments, "summary")
        reason = _optional_str(arguments, "reason")
        session_id = _optional_str(arguments, "session_id")
        tests = _test_list(arguments.get("tests"))
        changed_files = _str_list(arguments.get("changed_files"), "changed_files")
        runtime = self._resolve(arguments.get("project"))
        try:
            checkpoint = runtime.services.checkpoints.create_checkpoint(
                task_id,
                summary=summary,
                reason=reason,
                session_id=session_id,
                source=self._source,
                tests=tests,
                agent_summary=summary,
                agent_changed_files=changed_files,
            )
        except ValidationError as exc:
            raise ToolError(INVALID_ARGUMENT, str(exc)) from exc
        except PelError as exc:
            raise tool_error_from_exception(exc) from exc

        created = _find_event(
            runtime,
            lambda e: e.kind == "checkpoint_created" and e.checkpoint_id == checkpoint.id,
        )
        payload = created.payload if created is not None else {}
        agent_changed = payload.get("agent_changed_files")
        return {
            "project": self._project_block(runtime),
            "task_id": task_id,
            "checkpoint_id": checkpoint.id,
            "index": checkpoint.index,
            "checkpoint_saved": True,  # 走到这里事实一定已落盘（partial success 语义）
            "analysis_status": runtime.analysis_status(),
            "analysis_mode": "async" if runtime.worker is not None else "disabled",
            "evidence": {
                "base_ref": payload.get("base_ref"),
                "head_ref": payload.get("head_ref"),
                "scope_confidence": payload.get("scope_confidence"),
                "diff_evidence_id": payload.get("diff_evidence_id"),
                "changed_files": _file_event_paths(runtime, checkpoint.id),
                "agent_changed_files": list(agent_changed) if agent_changed else [],
            },
        }

    def complete_task(self, arguments: Mapping[str, Any], **_extra: Any) -> dict[str, Any]:
        task_id = _required_str(arguments, "task_id")
        summary = _optional_str(arguments, "summary")
        reason = _optional_str(arguments, "reason")
        tests = _test_list(arguments.get("tests"))
        runtime = self._resolve(arguments.get("project"))
        services = runtime.services
        try:
            task = services.tasks.get_task(task_id)
            if task is None:
                raise ToolError(TASK_NOT_FOUND, f"task not found: {task_id}")
            if task.status == "completed":
                return {
                    "project": self._project_block(runtime),
                    "task_id": task_id,
                    "status": "completed",
                    "checkpoint_id": None,
                    "analysis_status": "none",
                    "already_completed": True,
                }
            # 最后一个增量 checkpoint（语义分析异步），随后关闭 Task
            checkpoint = services.checkpoints.create_checkpoint(
                task_id,
                summary=summary,
                reason=reason,
                source=self._source,
                tests=tests,
                agent_summary=summary,
            )
            try:
                services.tasks.complete_task(task_id, summary=summary, source=self._source)
            except StorageError as exc:
                raise ToolError(
                    LEDGER_WRITE_FAILED,
                    f"final checkpoint {checkpoint.id} saved but task completion "
                    f"failed: {exc}",
                ) from exc
        except ToolError:
            raise
        except ValidationError as exc:
            raise ToolError(INVALID_ARGUMENT, str(exc)) from exc
        except PelError as exc:
            raise tool_error_from_exception(exc) from exc
        return {
            "project": self._project_block(runtime),
            "task_id": task_id,
            "status": "completed",
            "checkpoint_id": checkpoint.id,
            "analysis_status": runtime.analysis_status(),
            "already_completed": False,
        }

    # ----------------------------------------------------------------- query

    def get_project_state(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        scope = arguments.get("scope", "summary")
        if scope != "summary":
            raise ToolError(
                INVALID_ARGUMENT, f"unsupported scope {scope!r} (V0.1 only 'summary')"
            )
        runtime = self._resolve(arguments.get("project"))
        projection = self._catch_up(runtime)
        state = runtime.query.get_project_state()
        return {
            "project": self._project_block(runtime),
            "latest_version": state.latest_version,
            "active_tasks": [
                brief.model_dump(mode="json")
                for brief in runtime.query.list_active_tasks()
            ],
            "active_features": state.active_features,
            "current_parameters": state.current_parameters,
            "active_decisions": state.active_decisions,
            "recent_bug_fixes": state.recent_bug_fixes,
            "projection": projection,
        }

    def get_history(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        kind = _required_str(arguments, "kind")
        if kind not in _HISTORY_KINDS:
            raise ToolError(
                INVALID_ARGUMENT, f"kind must be one of {_HISTORY_KINDS}, got {kind!r}"
            )
        query_value = _optional_str(arguments, "query")
        limit = arguments.get("limit", DEFAULT_HISTORY_LIMIT)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_HISTORY_LIMIT
        ):
            raise ToolError(
                INVALID_ARGUMENT,
                f"'limit' must be an integer in [1, {MAX_HISTORY_LIMIT}]",
            )
        runtime = self._resolve(arguments.get("project"))
        projection = self._catch_up(runtime)
        service = runtime.query

        if kind == "version":
            return {
                "project": self._project_block(runtime),
                "kind": kind,
                "query": query_value,
                "count": 0,
                "results": [],
                "note": "version history is not available in V0.1",
                "projection": projection,
            }
        if kind == "task":
            if not query_value:
                raise ToolError(INVALID_ARGUMENT, "'query' (task_id) is required for kind=task")
            history = service.get_task_history(query_value)
            if history is None:
                raise ToolError(TASK_NOT_FOUND, f"task not found: {query_value}")
            return _history_response(
                self._project_block(runtime), kind, query_value,
                [history.model_dump(mode="json")], projection,
            )
        if kind == "file":
            if not query_value:
                raise ToolError(
                    INVALID_ARGUMENT, "'query' (file path) is required for kind=file"
                )
            file_history = service.get_file_history(query_value)
            return _history_response(
                self._project_block(runtime), kind, query_value,
                [file_history.model_dump(mode="json")], projection,
            )
        if kind in ("parameter", "feature", "decision") and not query_value:
            raise ToolError(
                INVALID_ARGUMENT, f"'query' (name/subject) is required for kind={kind}"
            )

        if kind == "parameter":
            parameter = service.get_parameter_history(query_value or "")
            results = [parameter.model_dump(mode="json")]
        elif kind == "feature":
            feature = service.get_feature_history(query_value or "")
            results = [feature.model_dump(mode="json")]
        elif kind == "decision":
            decision = service.get_decision_history(query_value or "")
            results = [decision.model_dump(mode="json")]
        elif kind == "bug":
            items = service.get_bug_history()
            if query_value:
                needle = query_value.lower()
                items = [
                    item
                    for item in items
                    if needle in item.problem.lower() or needle in item.fix.lower()
                ]
            results = _clip_list(
                [item.model_dump(mode="json") for item in items], limit
            )
        else:  # _HISTORY_KINDS 已在入口校验，这里只可能是程序性遗漏
            raise ToolError(INVALID_ARGUMENT, f"unsupported history kind: {kind!r}")
        if kind in ("parameter", "feature", "decision"):
            results = _clip_history_lists(results, limit)
        return _history_response(
            self._project_block(runtime), kind, query_value, results, projection
        )

    def get_evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        evidence_id = _required_str(arguments, "evidence_id")
        runtime = self._resolve(arguments.get("project"))
        if evidence_id.startswith("EVD-"):
            patch = (
                runtime.root / PEL_DIR_NAME / "evidence" / "diffs" / f"{evidence_id}.patch"
            )
            if not patch.is_file():
                raise ToolError(EVIDENCE_NOT_FOUND, f"evidence not found: {evidence_id}")
            raw = patch.read_text(encoding="utf-8", errors="replace")
            truncated = len(raw) > MAX_EVIDENCE_CHARS
            content = raw[:MAX_EVIDENCE_CHARS]
            referer = _find_event(
                runtime,
                lambda e: e.payload.get("diff_evidence_id") == evidence_id
                if isinstance(e.payload, dict)
                else False,
            )
            sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            return {
                "project": self._project_block(runtime),
                "evidence_id": evidence_id,
                "type": "git_diff",
                "sha256": sha256,
                "size_bytes": len(raw.encode("utf-8")),
                "path": patch.relative_to(runtime.root).as_posix(),
                "task_id": referer.task_id if referer else None,
                "checkpoint_id": referer.checkpoint_id if referer else None,
                "truncated": truncated,
                "content": content,
            }
        event = _find_event(runtime, lambda e: e.id == evidence_id)
        if event is None:
            raise ToolError(EVIDENCE_NOT_FOUND, f"evidence not found: {evidence_id}")
        return {
            "project": self._project_block(runtime),
            "evidence_id": evidence_id,
            "type": "raw_event",
            "kind": event.kind,
            "timestamp": event.timestamp.isoformat(),
            "task_id": event.task_id,
            "checkpoint_id": event.checkpoint_id,
            "session_id": event.session_id,
            "payload": event.payload,
        }

    # ---------------------------------------------------------------- inside

    def _catch_up(self, runtime: ProjectRuntime) -> dict[str, Any]:
        """查询前增量追平投影；追平失败不阻塞查询，但明确标记 stale。"""
        warning = None
        try:
            update_projection(runtime.root, config=runtime.config)
        except PelError as exc:
            _LOG.warning("projection catch-up failed for %s: %s", runtime.root, exc)
            warning = f"projection catch-up failed: {exc}"
        status: ProjectionStatus = runtime.query.status()
        block: dict[str, Any] = {
            "version": status.version,
            "stale": status.stale,
            "in_sync": status.in_sync,
        }
        if warning is not None:
            block["warning"] = warning
        return block

    def search_events(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """语义召回（retrieval extra）：按相关度返回事件引用，不生成内容。"""
        query = _required_str(arguments, "query")
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ToolError(INVALID_ARGUMENT, "'limit' must be an integer in [1, 50]")
        runtime = self._resolve(arguments.get("project"))
        projection = self._catch_up(runtime)
        from project_evolution.retrieval.embedder import RetrievalError

        try:
            hits = runtime.query.search_events(query, limit=limit)
        except RetrievalError as exc:
            raise ToolError(INTERNAL_ERROR, f"semantic search unavailable: {exc}") from exc
        return {
            "project": self._project_block(runtime),
            "query": query,
            "count": len(hits),
            "results": [hit.model_dump(mode="json") for hit in hits],
            "projection": projection,
        }


# ------------------------------------------------------------------ helpers


def _optional_str(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(INVALID_ARGUMENT, f"{key!r} must be a non-empty string or null")
    return value


def _required_str(arguments: Mapping[str, Any], key: str) -> str:
    value = _optional_str(arguments, key)
    if value is None:
        raise ToolError(INVALID_ARGUMENT, f"{key!r} is required")
    return value


def _test_list(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ToolError(INVALID_ARGUMENT, "'tests' must be an array of {command, status}")
    for item in value:
        if not isinstance(item, Mapping):
            raise ToolError(INVALID_ARGUMENT, "each test entry must be an object")
    return [dict(item) for item in value]  # 深度校验由 CheckpointService 完成


def _str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ToolError(INVALID_ARGUMENT, f"{key!r} must be an array of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ToolError(INVALID_ARGUMENT, f"{key!r} entries must be non-empty strings")
        items.append(item)
    return items


def _find_event(runtime: ProjectRuntime, predicate: Callable[[RawEvent], bool]) -> RawEvent | None:
    for event in runtime.services.store.replay():
        if predicate(event):
            return event
    return None


def _file_event_paths(runtime: ProjectRuntime, checkpoint_id: str) -> list[str]:
    kinds = ("file_added", "file_modified", "file_deleted")
    return [
        event.payload["path"]
        for event in runtime.services.store.replay()
        if event.kind in kinds and event.checkpoint_id == checkpoint_id
    ]


def _clip_list(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return items[:limit]


def _clip_history_lists(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """parameter/feature/decision 的 changes/decisions 列表按 limit 裁剪（保留最近）。"""
    clipped = []
    for result in results:
        item = dict(result)
        for key in ("changes", "decisions"):
            values = item.get(key)
            if isinstance(values, list) and len(values) > limit:
                item[key] = values[-limit:]
                item["truncated"] = True
        clipped.append(item)
    return clipped


def _history_response(
    project: dict[str, Any],
    kind: str,
    query_value: str | None,
    results: list[dict[str, Any]],
    projection: dict[str, Any],
) -> dict[str, Any]:
    count = len(results)
    if len(results) == 1:
        inner = results[0].get("changes") or results[0].get("decisions")
        if isinstance(inner, list):
            count = len(inner)
    return {
        "project": project,
        "kind": kind,
        "query": query_value,
        "count": count,
        "results": results,
        "projection": projection,
    }
