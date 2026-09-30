"""后台分析 worker 测试（phase-05 §3 默认异步，05 §16）。

- create_checkpoint 立即返回，语义分析在后台执行；
- 后台失败进入 pending，worker 不抛出、不阻塞事实采集；
- drain/stop 语义；重复 submit 由 analysis_key 幂等兜底。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from tests.gitrepo import commit_all, init_repo

from project_evolution.evolution import (
    AnalyzerResponse,
    CandidateEvent,
    MockProvider,
    ProviderError,
)
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.ledger import iter_evolution_events

PRJ = "PRJ-worker"


class BlockingProvider:
    """阻塞到外部 release 的确定性 Provider，用于证明 checkpoint 不等模型。"""

    name = "blocking"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    async def analyze(self, request):
        self.started.set()
        assert self.release.wait(timeout=10), "test never released the provider"
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


class SlowFailingProvider:
    name = "slow-failing"

    def __init__(self, delay: float = 0.2):
        self.delay = delay

    async def analyze(self, request):
        time.sleep(self.delay)
        raise ProviderError("model API failure", kind="api")


def setup_repo(tmp_path: Path):
    repo = init_repo(tmp_path)
    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.parent.mkdir()
    service_file.write_text(
        "class W {\n    private const double OpacityMin = 0.2;\n}\n", encoding="utf-8"
    )
    commit_all(repo, "init")
    return repo, service_file


def test_checkpoint_returns_before_analysis_completes(tmp_path: Path) -> None:
    """默认异步：create_checkpoint 在模型仍在阻塞时就已返回。"""
    repo, service_file = setup_repo(tmp_path)
    provider = BlockingProvider()
    services = create_git_aware_task_services(repo, project_id=PRJ, analysis_provider=provider)
    task = services.tasks.start_task(PRJ, "增加窗口透明度", agent="codex")

    service_file.write_text(
        "class W {\n    private const double OpacityMin = 0.3;\n}\n", encoding="utf-8"
    )
    checkpoint = services.checkpoints.create_checkpoint(task.id, summary="0.2 -> 0.3")

    # Checkpoint 事实已落盘；模型仍在阻塞中，语义事件尚未生成
    assert services.checkpoints.get_checkpoint(checkpoint.id) is not None
    assert provider.started.wait(timeout=10)
    assert list(iter_evolution_events(repo)) == []

    # 放行模型 -> drain 后语义事件出现
    provider.release.set()
    assert services.analysis_worker.wait_idle(timeout=10)
    events = list(iter_evolution_events(repo))
    assert len(events) == 1
    assert events[0].type == "parameter_change"
    assert events[0].checkpoint_id == checkpoint.id

    services.analysis_worker.stop()


def test_background_failure_goes_to_pending_without_raising(tmp_path: Path) -> None:
    repo, service_file = setup_repo(tmp_path)
    services = create_git_aware_task_services(
        repo, project_id=PRJ, analysis_provider=SlowFailingProvider(delay=0.1)
    )
    task = services.tasks.start_task(PRJ, "t", agent="codex")
    service_file.write_text(
        "class W {\n    private const double OpacityMin = 0.3;\n}\n", encoding="utf-8"
    )

    # 不抛出：Coding Agent 的任务不因 Analyzer 失败而失败
    checkpoint = services.checkpoints.create_checkpoint(task.id, summary="cp")
    assert services.checkpoints.get_checkpoint(checkpoint.id) is not None

    assert services.analysis_worker.wait_idle(timeout=10)
    worker = services.analysis_worker
    assert worker.failures and "API failure" in worker.failures[0]
    pending_dir = repo / ".project-evolution" / ".runtime" / "pending"
    assert len(list(pending_dir.glob("*.json"))) == 1
    # Raw Ledger 不回滚
    raw = (repo / ".project-evolution" / "events" / "raw.jsonl").read_text("utf-8")
    assert "checkpoint_created" in raw
    worker.stop()


def test_worker_retries_pending_with_mock_then_resolves(tmp_path: Path) -> None:
    """后台失败 -> pending -> 换可用 provider 的 retry_pending 可恢复。"""
    from project_evolution.evolution import EvolutionAnalyzerService

    repo, service_file = setup_repo(tmp_path)
    services = create_git_aware_task_services(
        repo, project_id=PRJ, analysis_provider=SlowFailingProvider(delay=0.05)
    )
    task = services.tasks.start_task(PRJ, "t", agent="codex")
    service_file.write_text(
        "class W {\n    private const double OpacityMin = 0.3;\n}\n", encoding="utf-8"
    )
    services.checkpoints.create_checkpoint(task.id, summary="cp")
    assert services.analysis_worker.wait_idle(timeout=10)
    services.analysis_worker.stop()

    # 换可用 provider 重试
    analyzer = EvolutionAnalyzerService(repo, MockProvider())
    analyzer.retry_pending()
    assert analyzer.pending.list_pending() == []

    from project_evolution.ledger import iter_evolution_events as it

    assert list(it(repo)) == []  # mock 默认 NOOP：不产生事件，但 pending 清空


def test_duplicate_submit_is_idempotent(tmp_path: Path) -> None:
    """同一 checkpoint 重复 submit：analysis_key 幂等，只产生一批事件。"""
    repo, service_file = setup_repo(tmp_path)
    provider = BlockingProvider()
    services = create_git_aware_task_services(repo, project_id=PRJ, analysis_provider=provider)
    task = services.tasks.start_task(PRJ, "t", agent="codex")
    service_file.write_text(
        "class W {\n    private const double OpacityMin = 0.3;\n}\n", encoding="utf-8"
    )
    checkpoint = services.checkpoints.create_checkpoint(task.id, summary="cp")
    worker = services.analysis_worker
    assert provider.started.wait(timeout=10)
    worker.submit(task.id, checkpoint.id)  # 手动重复提交
    worker.submit(task.id, checkpoint.id)
    provider.release.set()
    assert worker.wait_idle(timeout=10)

    events = list(iter_evolution_events(repo))
    assert len(events) == 1
    assert len({event.analysis_batch_id for event in events}) == 1
    worker.stop()
