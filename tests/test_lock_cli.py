"""写锁与 pel CLI 测试。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from project_evolution.cli import main
from project_evolution.ledger import LockTimeoutError, RawEventLedger, write_lock
from project_evolution.ledger.jsonl import encode_record
from project_evolution.models import RawEvent
from test_raw_ledger import NOW, line_for, raw_event  # noqa: F401 - 复用测试工厂


def test_write_lock_is_exclusive_then_releasable(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "ledger.lock"
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with write_lock(path, timeout=1.0):
            acquired.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(timeout=5)
    with pytest.raises(LockTimeoutError), write_lock(path, timeout=0.3):
        pass
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    # 释放后可以再次获取
    with write_lock(path, timeout=1.0):
        pass


# --------------------------------------------------------------------- CLI


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("pel ")


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_doctor_on_empty_project_is_ok(tmp_path: Path, capsys) -> None:
    assert main(["doctor", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ok=True" in out
    assert "LEDGER_MISSING" in out


def test_doctor_on_missing_root_returns_2(tmp_path: Path) -> None:
    assert main(["doctor", "--project", str(tmp_path / "nope")]) == 2


def test_doctor_detects_and_repairs_tail(tmp_path: Path, capsys) -> None:
    ledger = RawEventLedger(tmp_path)
    ledger.append_many([raw_event(), raw_event()])
    good = ledger.events_path.read_bytes()
    ledger.events_path.write_bytes(good + b'{"id":"RAW-xxx"')

    rc = main(["doctor", "--project", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ok=False" in out
    assert "LEDGER_TAIL_CORRUPTION" in out
    assert ledger.events_path.read_bytes().endswith(b'{"id":"RAW-xxx"')  # 未修复前不动文件

    rc = main(["doctor", "--project", str(tmp_path), "--repair"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "repair: status=repaired" in out
    assert ledger.events_path.read_bytes() == good
    assert RawEventLedger(tmp_path).check_integrity().ok


def test_doctor_json_output(tmp_path: Path, capsys) -> None:
    assert main(["doctor", "--project", str(tmp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"valid_events": 0' in out


def test_doctor_honors_config_events_dir(tmp_path: Path, capsys) -> None:
    pel_dir = tmp_path / ".project-evolution"
    pel_dir.mkdir()
    (pel_dir / "config.yaml").write_text("events_dir: events-alt\n", encoding="utf-8")
    custom = pel_dir / "events-alt" / "raw.jsonl"
    custom.parent.mkdir(parents=True)
    event = raw_event()
    custom.write_text(
        encode_record(
            RawEvent(
                **{**event.model_dump(), "seq": 1}
            ).model_dump(mode="json")
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["doctor", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "events-alt" in out.replace("\\", "/")
    assert "valid_events=1" in out


def test_doctor_middle_corruption_not_repaired(tmp_path: Path, capsys) -> None:
    ledger = RawEventLedger(tmp_path)
    e1 = ledger.append(raw_event())
    corrupt = line_for(e1, 1) + b"not json\n"
    ledger.events_path.write_bytes(corrupt)

    rc = main(["doctor", "--project", str(tmp_path), "--repair"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "repair: status=blocked" in out
    assert ledger.events_path.read_bytes() == corrupt
