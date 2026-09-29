"""pel CLI。

Phase 01：仅入口与 ``--version``。
Phase 02：增加 ``pel doctor``，暴露 Ledger 的 integrity / repair 能力。
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _doctor(args, parser)
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
