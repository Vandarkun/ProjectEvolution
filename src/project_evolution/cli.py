"""pel CLI。

Phase 01：仅入口与 ``--version``。
Phase 02：增加 ``pel doctor``，暴露 Ledger 的 integrity / repair 能力。
Phase 05：增加 ``pel analyze``（同步语义分析，测试/CLI 场景）与
``pel retry-pending``（重试失败分析）；产品默认路径是后台异步 worker。
Phase 06：增加 ``pel index rebuild``（SQLite 投影按 seq 全量重建，
07-storage-and-index.md §4）。
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
    return parser


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
