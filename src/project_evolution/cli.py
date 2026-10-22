"""pel CLI。

Phase 01：仅入口与 ``--version``。
Phase 02：增加 ``pel doctor``，暴露 Ledger 的 integrity / repair 能力。
Phase 05：增加 ``pel analyze``（同步语义分析，测试/CLI 场景）与
``pel retry-pending``（重试失败分析）；产品默认路径是后台异步 worker。
Phase 06：增加 ``pel index rebuild``（SQLite 投影按 seq 全量重建，
07-storage-and-index.md §4）。
Phase 07：增加 ``pel mcp serve``（stdio MCP Server）、``pel mcp
agent-instructions``（最短 Agent 规则样例）与 ``pel task start/checkpoint/
complete``（Shell-only Agent 的兼容入口，输出 JSON；语义分析与 MCP 相同
默认异步，命令结束前 drain 后台 worker）。
Phase 08：增加 ``pel export``（Markdown/HTML Evolution Report，可独立
生成）与 ``pel dashboard``（本地只读 FastAPI Dashboard，默认仅监听
loopback；需要 ``[dashboard]`` extra）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from project_evolution import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pel", description="Project Evolution Ledger (PEL)"
    )
    parser.add_argument("--version", action="version", version=f"pel {__version__}")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser(
        "doctor", help="check raw ledger integrity; --repair truncates a corrupt tail"
    )
    doctor.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    doctor.add_argument(
        "--repair", action="store_true", help="truncate an unconfirmed corrupt tail"
    )
    doctor.add_argument(
        "--json", dest="as_json", action="store_true", help="output machine-readable JSON"
    )

    analyze = sub.add_parser(
        "analyze", help="analyze one checkpoint synchronously (mock by default)"
    )
    analyze.add_argument("task_id", help="task id (TASK-...)")
    analyze.add_argument("checkpoint_id", help="checkpoint id (CHK-...)")
    _add_provider_args(analyze)
    analyze.add_argument(
        "--json", dest="as_json", action="store_true", help="output machine-readable JSON"
    )

    retry = sub.add_parser(
        "retry-pending", help="retry pending analyses saved after provider failures"
    )
    _add_provider_args(retry)
    retry.add_argument(
        "--json", dest="as_json", action="store_true", help="output machine-readable JSON"
    )

    index = sub.add_parser("index", help="SQLite projection index (rebuild from ledgers)")
    index_sub = index.add_subparsers(dest="index_command")
    index_rebuild = index_sub.add_parser(
        "rebuild", help="rebuild the SQLite projection by replaying ledgers in seq order"
    )
    index_rebuild.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    index_rebuild.add_argument(
        "--json", dest="as_json", action="store_true", help="output machine-readable JSON"
    )

    mcp = sub.add_parser("mcp", help="MCP integration (phase-07)")
    mcp_sub = mcp.add_subparsers(dest="mcp_command")
    mcp_serve = mcp_sub.add_parser("serve", help="run the stdio MCP server")
    mcp_serve.add_argument(
        "--project",
        type=Path,
        default=None,
        help="pin a single project root (default: cwd if it has .project-evolution/)",
    )
    mcp_serve.add_argument(
        "--registry", type=Path, default=None, help="multi-project registry JSON file"
    )
    _add_provider_args(mcp_serve, with_project=False)
    mcp_serve.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="WARNING",
        help="diagnostics go to stderr only (default WARNING)",
    )
    mcp_instructions = mcp_sub.add_parser(
        "agent-instructions", help="print the shortest AGENTS.md rule sample"
    )
    mcp_instructions.add_argument(
        "--project", default=None, help="project name hint for multi-project servers"
    )

    task = sub.add_parser(
        "task", help="task lifecycle for shell-only agents (JSON output)"
    )
    task_sub = task.add_subparsers(dest="task_command")
    task_start = task_sub.add_parser("start", help="start a task and capture git baseline")
    task_start.add_argument("title", help="task title")
    task_start.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    task_start.add_argument("--agent", default=None, help="agent name, e.g. codex")
    task_start.add_argument("--session-id", dest="session_id", default=None)
    task_start.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        help="skip draining background analysis before exit",
    )
    task_checkpoint = task_sub.add_parser(
        "checkpoint", help="append a checkpoint with facts and evidence"
    )
    task_checkpoint.add_argument("task_id", help="task id (TASK-...)")
    task_checkpoint.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    task_checkpoint.add_argument("--summary", default=None)
    task_checkpoint.add_argument("--reason", default=None)
    task_checkpoint.add_argument(
        "--test",
        dest="tests",
        action="append",
        default=[],
        metavar="COMMAND=STATUS",
        help="test hint, e.g. 'pytest -q=passed' (repeatable)",
    )
    _add_provider_args(task_checkpoint, with_project=False)
    task_checkpoint.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        help="skip waiting for background analysis before exit",
    )
    task_complete = task_sub.add_parser(
        "complete", help="final checkpoint + close the task"
    )
    task_complete.add_argument("task_id", help="task id (TASK-...)")
    task_complete.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    task_complete.add_argument("--summary", default=None)
    task_complete.add_argument(
        "--test",
        dest="tests",
        action="append",
        default=[],
        metavar="COMMAND=STATUS",
        help="test hint, e.g. 'pytest -q=passed' (repeatable)",
    )
    _add_provider_args(task_complete, with_project=False)
    task_complete.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        help="skip waiting for background analysis before exit",
    )

    search = sub.add_parser(
        "search", help="semantic search over evolution events (retrieval extra)"
    )
    search.add_argument("query", help="natural language query")
    search.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    search.add_argument("--limit", type=int, default=10, help="max hits (default 10)")
    search.add_argument(
        "--json", dest="as_json", action="store_true", help="machine-readable JSON output"
    )

    export = sub.add_parser(
        "export", help="export the evolution report as markdown or html"
    )
    export.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    export.add_argument(
        "--format",
        dest="export_format",
        choices=["markdown", "html"],
        default="markdown",
        help="report format (default markdown)",
    )
    export.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write to a file instead of stdout",
    )

    dashboard = sub.add_parser(
        "dashboard", help="run the local read-only dashboard (needs [dashboard] extra)"
    )
    dashboard.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1, loopback only)",
    )
    dashboard.add_argument("--port", type=int, default=8642)
    return parser


def _add_provider_args(
    parser: argparse.ArgumentParser, *, with_project: bool = True
) -> None:
    if with_project:
        parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument(
        "--provider",
        choices=["mock", "openai", "ollama"],
        default="mock",
        help="evolution model provider (default: mock)",
    )
    parser.add_argument("--model", help="model name (default: config analyzer_model)")
    parser.add_argument("--base-url", help="provider base URL")
    parser.add_argument(
        "--api-key-env", help="environment variable holding the API key (openai only)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _doctor(args, parser)
    if args.command == "analyze":
        return _analyze(args)
    if args.command == "retry-pending":
        return _retry_pending(args)
    if args.command == "index":
        if args.index_command == "rebuild":
            return _index_rebuild(args)
        parser.error("index requires a subcommand (rebuild)")
    if args.command == "mcp":
        if args.mcp_command == "serve":
            return _mcp_serve(args)
        if args.mcp_command == "agent-instructions":
            return _agent_instructions(args)
        parser.error("mcp requires a subcommand (serve | agent-instructions)")
    if args.command == "task":
        if args.task_command == "start":
            return _task_command(args, "start_task")
        if args.task_command == "checkpoint":
            return _task_command(args, "checkpoint_task")
        if args.task_command == "complete":
            return _task_command(args, "complete_task")
        parser.error("task requires a subcommand (start | checkpoint | complete)")
    if args.command == "search":
        return _search(args)
    if args.command == "export":
        return _export(args)
    if args.command == "dashboard":
        return _dashboard(args)
    return 2  # argparse 只会产生已注册子命令，此处不可达


def _doctor(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from project_evolution.config import PEL_DIR_NAME, load_config
    from project_evolution.errors import ConfigError, ProjectNotFoundError
    from project_evolution.ledger import RawEventLedger

    root: Path = args.project
    try:
        if not root.is_dir():
            raise ProjectNotFoundError(f"project root not found: {root}")
        config = load_config(root)
        ledger = RawEventLedger(
            root,
            events_path=root / PEL_DIR_NAME / config.events_dir / "raw.jsonl",
            lock_path=root / PEL_DIR_NAME / config.runtime_dir / "locks" / "ledger.lock",
        )
        report = ledger.check_integrity()
        if args.repair:
            repair = ledger.repair_tail()
            report = ledger.check_integrity()
        else:
            repair = None
    except (ConfigError, ProjectNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        payload: dict[str, object] = {
            "ok": report.ok,
            "path": report.path,
            "total_lines": report.total_lines,
            "valid_events": report.valid_events,
            "tail_corrupt": report.tail_corrupt,
            "issues": [issue.model_dump() for issue in report.issues],
        }
        if repair is not None:
            payload["repair"] = repair.model_dump()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ledger: {report.path}")
        print(
            f"ok={report.ok} valid_events={report.valid_events} "
            f"total_lines={report.total_lines} tail_corrupt={report.tail_corrupt}"
        )
        for issue in report.issues:
            loc = f"line {issue.line}: " if issue.line is not None else ""
            print(f"  [{issue.severity}] {loc}{issue.code}: {issue.message}")
        if repair is not None:
            print(
                f"repair: status={repair.status} truncated_bytes={repair.truncated_bytes} "
                f"({repair.original_size} -> {repair.new_size}) {repair.message}"
            )
    return 0 if report.ok else 1


def _index_rebuild(args: argparse.Namespace) -> int:
    from project_evolution.errors import ConfigError, PelError, ProjectNotFoundError
    from project_evolution.index import rebuild_projection

    try:
        if not args.project.is_dir():
            raise ProjectNotFoundError(f"project root not found: {args.project}")
        report = rebuild_projection(args.project)
    except (ConfigError, ProjectNotFoundError, PelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"projection: {report.db_path}")
        print(
            f"version={report.version} (was {report.previous_version}) "
            f"in {report.duration_ms:.0f}ms"
        )
        for table, count in report.counts.items():
            print(f"  {table}: {count}")
        if report.removed_old_files:
            print(f"cleaned {len(report.removed_old_files)} old projection file(s)")
    return 0


def _build_provider(args: argparse.Namespace):
    """按 CLI 参数 + 项目配置组装 Provider（mock 默认，真实 Provider 显式选择）。"""
    import os

    from project_evolution.config import load_config
    from project_evolution.evolution import (
        MockProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
    )

    config = load_config(args.project)
    model = args.model or config.analyzer_model
    if args.provider == "mock":
        return MockProvider()
    if args.provider == "openai":
        if not model:
            print(
                "error: --model (or config analyzer_model) is required for openai provider",
                file=sys.stderr,
            )
            return None
        env_name = args.api_key_env or config.analyzer_api_key_env
        api_key = os.environ.get(env_name) if env_name else None
        return OpenAICompatibleProvider(
            model=model,
            base_url=args.base_url or "https://api.openai.com/v1",
            api_key=api_key,
        )
    if not model:
        print(
            "error: --model (or config analyzer_model) is required for ollama provider",
            file=sys.stderr,
        )
        return None
    return OllamaProvider(
        model=model, base_url=args.base_url or "http://localhost:11434"
    )


def _analyze(args: argparse.Namespace) -> int:
    from project_evolution.errors import ConfigError, PelError, ProjectNotFoundError
    from project_evolution.evolution import EvolutionAnalyzerService

    try:
        if not args.project.is_dir():
            raise ProjectNotFoundError(f"project root not found: {args.project}")
        provider = _build_provider(args)
        if provider is None:
            return 2
        analyzer = EvolutionAnalyzerService(args.project, provider)
        outcome = analyzer.analyze_checkpoint(args.task_id, args.checkpoint_id)
    except (ConfigError, ProjectNotFoundError, PelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "pending" in str(exc).lower():
            return 1
        return 2

    if args.as_json:
        print(outcome.model_dump_json(indent=2))
    else:
        print(
            f"analysis {outcome.analysis_id}: status={outcome.status} "
            f"events={len(outcome.events)} provider={outcome.provider or '-'}"
        )
        for event in outcome.events:
            print(f"  {event.type}: {event.title}")
        for rejection in outcome.rejections:
            print(f"  [rejected] {rejection.code}: {rejection.message}")
        for _index, reason in outcome.deduped:
            print(f"  [deduped] {reason}")
    return 0


def _retry_pending(args: argparse.Namespace) -> int:
    from project_evolution.errors import ConfigError, PelError, ProjectNotFoundError
    from project_evolution.evolution import EvolutionAnalyzerService

    try:
        if not args.project.is_dir():
            raise ProjectNotFoundError(f"project root not found: {args.project}")
        provider = _build_provider(args)
        if provider is None:
            return 2
        analyzer = EvolutionAnalyzerService(args.project, provider)
        results = analyzer.retry_pending()
    except (ConfigError, ProjectNotFoundError, PelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(
            json.dumps(
                {
                    "total": len(results),
                    "succeeded": sum(1 for r in results if r.succeeded),
                    "results": [
                        {
                            "analysis_id": r.pending.analysis_id,
                            "task_id": r.pending.task_id,
                            "checkpoint_id": r.pending.checkpoint_id,
                            "status": r.outcome.status if r.outcome else "failed",
                            "error": r.error,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"retried {len(results)} pending analysis(es)")
        for result in results:
            if result.succeeded and result.outcome is not None:
                outcome = result.outcome
                print(
                    f"  ok {result.pending.analysis_id}: status={outcome.status} "
                    f"events={len(outcome.events)}"
                )
            else:
                print(
                    f"  still-failing {result.pending.analysis_id}: {result.error}"
                )
    return 0 if all(r.succeeded for r in results) else 1


# --------------------------------------------------------------- phase-07


def _mcp_serve(args: argparse.Namespace) -> int:
    """stdio MCP Server：stdout 只写协议，diagnostics 全部走 stderr。"""
    import logging as logging_module

    from project_evolution.config import PEL_DIR_NAME
    from project_evolution.mcp import (
        PelMcpServer,
        PelToolService,
        ProjectRegistry,
        ProviderSpec,
        build_provider,
        configure_logging,
    )
    from project_evolution.mcp.errors import ToolError
    from project_evolution.mcp.registry import INVALID_REGISTRY

    configure_logging(getattr(logging_module, args.log_level))
    try:
        if args.project is not None:
            registry = ProjectRegistry.single(args.project)
        elif args.registry is not None:
            registry = ProjectRegistry.load(args.registry)
        elif (Path.cwd() / PEL_DIR_NAME).is_dir():
            registry = ProjectRegistry.single(Path.cwd())
        else:
            print(
                "error: no project configured; pass --project ROOT or --registry FILE",
                file=sys.stderr,
            )
            return 2
        registry.validate_roots()
        spec = ProviderSpec(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
        service = PelToolService(
            registry,
            provider_factory=lambda config: build_provider(spec, config),
            source="mcp",
        )
        server = PelMcpServer(service)
    except ToolError as exc:
        code = exc.code if exc.code != INVALID_REGISTRY else "INVALID_REGISTRY"
        print(f"error: {code}: {exc.message}", file=sys.stderr)
        return 2
    return server.serve_forever()


def _agent_instructions(args: argparse.Namespace) -> int:
    from project_evolution.mcp.instructions import render_agent_instructions

    print(render_agent_instructions(project=args.project), end="")
    return 0


def _parse_test_hints(raw_tests: list[str]) -> list[dict[str, str]]:
    """``COMMAND=STATUS`` → ``{"command": ..., "status": ...}``。"""
    tests = []
    for raw in raw_tests:
        command, sep, status = raw.rpartition("=")
        if not sep or not command.strip() or status not in ("passed", "failed"):
            raise SystemExit(
                f"error: --test expects COMMAND=passed|failed, got {raw!r}"
            )
        tests.append({"command": command.strip(), "status": status})
    return tests


def _task_command(args: argparse.Namespace, tool: str) -> int:
    """``pel task start/checkpoint/complete``：同一 Tool Service 层的 CLI 兼容入口。

    checkpoint/complete 与 MCP 相同默认异步提交语义分析；CLI 的一次性进程
    在退出前默认 drain（08 §5：同步等待仅作为 CLI / 测试选项），并把
    ``analysis_status`` 刷新为最终结果（committed/noop/pending）。
    """
    from project_evolution.mcp import (
        PelToolService,
        ProjectRegistry,
        ProviderSpec,
        build_provider,
    )
    from project_evolution.mcp.errors import ToolError

    root: Path = args.project
    if not root.is_dir():
        print(f"error: project root not found: {root}", file=sys.stderr)
        return 2
    if tool == "start_task":
        spec = None  # start 不产生 checkpoint，无需 Provider
    else:
        spec = ProviderSpec(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    service = PelToolService(
        ProjectRegistry.single(root),
        provider_factory=(
            (lambda config: build_provider(spec, config)) if spec is not None else None
        ),
        source="cli",
    )
    try:
        if tool == "start_task":
            result = service.start_task(
                {"title": args.title, "agent": args.agent, "session_id": args.session_id}
            )
        elif tool == "checkpoint_task":
            result = service.checkpoint_task(
                {
                    "task_id": args.task_id,
                    "summary": args.summary,
                    "reason": args.reason,
                    "tests": _parse_test_hints(args.tests),
                }
            )
        else:
            result = service.complete_task(
                {
                    "task_id": args.task_id,
                    "summary": args.summary,
                    "tests": _parse_test_hints(args.tests),
                }
            )
        checkpoint_id = result.get("checkpoint_id")
        if (
            checkpoint_id
            and not args.no_wait
            and result.get("analysis_status") == "pending"
        ):
            service.drain_analysis()
            result["analysis_status"] = service.resolve_analysis_status(checkpoint_id)
    except ToolError as exc:
        print(
            json.dumps(exc.to_payload(), ensure_ascii=False, indent=2), file=sys.stderr
        )
        return 2
    finally:
        service.shutdown(timeout=5.0)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------- phase-08


def _search(args: argparse.Namespace) -> int:
    """``pel search``：语义召回事件引用（向量只定位，事实按 ID 下钻）。"""
    import json as _json

    from project_evolution.errors import PelError
    from project_evolution.index import ProjectionNotBuiltError, QueryService
    from project_evolution.retrieval.embedder import RetrievalError

    service = QueryService(args.project)
    try:
        hits = service.search_events(args.query, limit=max(1, args.limit))
    except RetrievalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProjectionNotBuiltError:
        print(
            "error: projection not built yet; run `pel index rebuild` first",
            file=sys.stderr,
        )
        return 2
    except PelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(_json.dumps([h.model_dump(mode="json") for h in hits], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("(no matching events)")
        return 0
    for h in hits:
        print(f"{h.score:+.4f}  {h.event_id}  [{h.type}] {h.title}")
    print(f"\n{len(hits)} hit(s); use `pel export` or get_history/get_evidence for details")
    return 0


def _export(args: argparse.Namespace) -> int:
    """pel export：Markdown / HTML Evolution Report（可独立生成，无需 Dashboard）。"""
    import contextlib

    from project_evolution.dashboard import build_report, render_html, render_markdown
    from project_evolution.errors import ConfigError, PelError, ProjectNotFoundError
    from project_evolution.index import ProjectionNotBuiltError, QueryService, update_projection

    try:
        if not args.project.is_dir():
            raise ProjectNotFoundError(f"project root not found: {args.project}")
        # 导出前增量追平投影；失败不阻塞（沿用 Dashboard/MCP 查询路径）
        with contextlib.suppress(PelError):
            update_projection(args.project)
        report = build_report(QueryService(args.project))
    except (ConfigError, ProjectNotFoundError, PelError, ProjectionNotBuiltError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    content = (
        render_markdown(report) if args.export_format == "markdown" else render_html(report)
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8", newline="\n")
        print(f"report written: {args.output}")
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    """pel dashboard：本地只读 Dashboard（默认仅监听 loopback，11 §9）。"""
    from project_evolution.dashboard import run_dashboard
    from project_evolution.errors import ConfigError, ProjectNotFoundError

    try:
        if not args.project.is_dir():
            raise ProjectNotFoundError(f"project root not found: {args.project}")
    except (ConfigError, ProjectNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        run_dashboard(args.project, host=args.host, port=args.port)
    except RuntimeError as exc:  # 缺 [dashboard] extra
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
