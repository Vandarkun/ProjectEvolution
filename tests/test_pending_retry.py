"""Pending queue 与 retry_pending 测试（phase-05 §9）+ analyze/retry-pending CLI。

- Provider timeout / API failure -> .runtime/pending/<ANL-id>.json，Raw Ledger 不回滚；
- retry_pending() 幂等：成功清除记录，失败保留并累加 attempts；
- CLI：pel analyze / pel retry-pending。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.gitrepo import commit_all, init_repo
from tests.test_analysis_closed_loop import setup_task_with_change

from project_evolution.cli import main as pel_main
from project_evolution.evolution import (
    AnalysisError,
    AnalyzerResponse,
    CandidateEvent,
    EvolutionAnalyzerService,
    ProviderError,
)
from project_evolution.ledger import iter_evolution_events

PRJ = "PRJ-pending"


class FlakyProvider:
    """前 N 次调用抛 ProviderError(timeout)，之后返回一个合法 parameter_change。"""

    name = "flaky"

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    async def analyze(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("simulated model timeout", kind="timeout")
        assert request.digest.evidence
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="OpacityMin 0.2 -> 0.3",
                    evidence_ids=[request.digest.evidence[0].id],
                    payload={"name": "OpacityMin", "before": "0.2", "after": "0.3"},
                )
            ]
        )


def raw_line_count(repo: Path) -> int:
    path = repo / ".project-evolution" / "events" / "raw.jsonl"
    return len(path.read_text(encoding="utf-8").splitlines())


def test_provider_failure_creates_pending_and_keeps_raw_ledger(tmp_path: Path) -> None:
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    before = raw_line_count(repo)
    analyzer = EvolutionAnalyzerService(repo, FlakyProvider(failures=1))

    with pytest.raises(ProviderError):
        analyzer.analyze_checkpoint(task.id, checkpoint.id)

    # Raw Ledger 不回滚（事实与证据已在）
    assert raw_line_count(repo) == before
    # pending 落盘
    pending_dir = repo / ".project-evolution" / ".runtime" / "pending"
    files = list(pending_dir.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["analysis_id"].startswith("ANL-")
    assert record["last_error_kind"] == "timeout"
    assert record["task_id"] == task.id and record["checkpoint_id"] == checkpoint.id
    assert record["attempts"] == 1
    assert files[0].name == f"{record['analysis_id']}.json"
    # 失败不写语义事件
    assert list(iter_evolution_events(repo)) == []


def test_retry_pending_recovers_and_clears_records(tmp_path: Path) -> None:
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    provider = FlakyProvider(failures=1)
    analyzer = EvolutionAnalyzerService(repo, provider)
    with pytest.raises(ProviderError):
        analyzer.analyze_checkpoint(task.id, checkpoint.id)

    results = analyzer.retry_pending()
    assert len(results) == 1 and results[0].succeeded
    assert results[0].outcome is not None and results[0].outcome.status == "committed"
    events = list(iter_evolution_events(repo))
    assert len(events) == 1
    # 成功后 pending 清空；再次 retry 无事可做
    assert analyzer.pending.list_pending() == []
    assert analyzer.retry_pending() == []
    # 幂等：不重复提交
    again = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert again.status == "existing"
    assert len(list(iter_evolution_events(repo))) == 1


def test_retry_pending_failure_keeps_record_with_more_attempts(tmp_path: Path) -> None:
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    analyzer = EvolutionAnalyzerService(repo, FlakyProvider(failures=99))
    with pytest.raises(ProviderError):
        analyzer.analyze_checkpoint(task.id, checkpoint.id)

    results = analyzer.retry_pending()
    assert len(results) == 1
    assert not results[0].succeeded
    assert results[0].error is not None and "timeout" in results[0].error
    pending = analyzer.pending.list_pending()
    assert len(pending) == 1 and pending[0].attempts == 2  # 初次 1 + 重试 1


def test_analysis_error_wraps_unexpected_provider_crash(tmp_path: Path) -> None:
    class CrashProvider:
        name = "crash"

        async def analyze(self, request):
            raise RuntimeError("boom")

    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    analyzer = EvolutionAnalyzerService(repo, CrashProvider())
    with pytest.raises(AnalysisError, match="boom"):
        analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert len(analyzer.pending.list_pending()) == 1


# ------------------------------------------------------------------- CLI


def test_cli_analyze_mock(tmp_path: Path, capsys) -> None:
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    code = pel_main(
        [
            "analyze",
            task.id,
            checkpoint.id,
            "--project",
            str(repo),
            "--provider",
            "mock",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "status=noop" in out  # 默认 MockProvider 无 responder -> events=[]
    # JSON 模式
    code = pel_main(
        [
            "analyze",
            task.id,
            checkpoint.id,
            "--project",
            str(repo),
            "--provider",
            "mock",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "existing"  # 第二次命中 NOOP 缓存


def test_cli_analyze_unknown_checkpoint_fails(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(repo, "init")
    code = pel_main(
        [
            "analyze",
            "TASK-nope",
            "CHK-nope",
            "--project",
            str(repo),
            "--provider",
            "mock",
        ]
    )
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_retry_pending(tmp_path: Path, capsys) -> None:
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    analyzer = EvolutionAnalyzerService(repo, FlakyProvider(failures=1))
    with pytest.raises(ProviderError):
        analyzer.analyze_checkpoint(task.id, checkpoint.id)

    code = pel_main(
        ["retry-pending", "--project", str(repo), "--provider", "mock"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "retried 1" in out
    assert analyzer.pending.list_pending() == []
