"""MockProvider 完整闭环测试（phase-05 §4）。

```text
Checkpoint -> ChangeDigest -> Provider -> Validator -> evolution.jsonl
```

在真实 Git 仓库上通过 TaskServices 建立 checkpoint，
再由 EvolutionAnalyzerService 完成语义分析与提交；
覆盖幂等（analysis_key）、NOOP、去重、Validator 拒绝路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.gitrepo import commit_all, init_repo

from project_evolution.evolution import (
    AnalyzerResponse,
    CandidateEvent,
    EvolutionAnalyzerService,
    MockProvider,
)
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.ledger import iter_evolution_events

PRJ = "PRJ-closed-loop"


def setup_task_with_change(tmp_path: Path):
    repo = init_repo(tmp_path)
    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.parent.mkdir()
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.2;\n"
        "}\n",
        encoding="utf-8",
    )
    commit_all(repo, "init")
    services = create_git_aware_task_services(repo, project_id=PRJ)
    task = services.tasks.start_task(PRJ, "增加窗口透明度", agent="codex")
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.3;\n"
        "}\n",
        encoding="utf-8",
    )
    checkpoint = services.checkpoints.create_checkpoint(
        task.id, summary="最低透明度 0.2 -> 0.3"
    )
    return repo, services, task, checkpoint


def parameter_responder(calls: list[int]):
    """返回引用 digest evidence 清单第一条的 parameter_change 候选。"""

    def respond(request) -> AnalyzerResponse:
        calls.append(1)
        assert request.digest.evidence, "digest must carry the checkpoint evidence manifest"
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="OpacityMin 0.2 -> 0.3",
                    description="提高最低可见度",
                    evidence_ids=[request.digest.evidence[0].id],
                    payload={
                        "name": "OpacityMin",
                        "before": "0.2",
                        "after": "0.3",
                        "reason": None,
                    },
                )
            ]
        )

    return respond


def test_mock_closed_loop_commits_to_evolution_jsonl(tmp_path: Path) -> None:
    repo, services, task, checkpoint = setup_task_with_change(tmp_path)
    calls: list[int] = []
    provider = MockProvider(responder=parameter_responder(calls))
    analyzer = EvolutionAnalyzerService(repo, provider)

    outcome = analyzer.analyze_checkpoint(task.id, checkpoint.id)

    assert outcome.status == "committed"
    assert len(outcome.events) == 1
    event = outcome.events[0]
    assert event.type == "parameter_change"
    assert event.payload["after"] == "0.3"
    assert event.analyzer_version and event.prompt_version == "evolution-v1"
    assert event.analysis_batch_id.startswith("ANL-")
    assert event.evidence_ids and event.evidence_ids[0].startswith("EVD-")

    # evolution.jsonl 落盘且可重放
    stored = list(iter_evolution_events(repo))
    assert [e.id for e in stored] == [event.id]
    assert stored[0].analysis_key == outcome.analysis_key
    assert stored[0].task_id == task.id and stored[0].checkpoint_id == checkpoint.id

    # Raw Ledger 不因分析而改变
    raw_lines = (repo / ".project-evolution" / "events" / "raw.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert raw_lines and all(json.loads(line)["id"].startswith("RAW-") for line in raw_lines)


def test_retry_is_idempotent_same_analysis_key(tmp_path: Path) -> None:
    """同一 Checkpoint 重试：analysis_key 命中，不重复调用 Provider、不重复追加。"""
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    calls: list[int] = []
    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=parameter_responder(calls)))

    first = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    second = analyzer.analyze_checkpoint(task.id, checkpoint.id)

    assert first.status == "committed"
    assert second.status == "existing"
    assert second.analysis_id == first.analysis_id
    assert second.events == first.events
    assert len(calls) == 1  # 幂等命中后不再调用模型
    assert len(list(iter_evolution_events(repo))) == 1


def test_noop_checkpoint_records_nothing_and_caches(tmp_path: Path) -> None:
    """events=[] 是正常结果：不写 evolution.jsonl，重试直接命中缓存。"""
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)
    calls: list[int] = []

    def noop(request) -> AnalyzerResponse:
        calls.append(1)
        return AnalyzerResponse(events=[], raw_output='{"events": []}')

    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=noop))
    first = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert first.status == "noop" and first.noop is True
    assert first.events == []
    assert not (repo / ".project-evolution" / "events" / "evolution.jsonl").exists()
    # NOOP 无事件落账，runtime 记录必须保留原始输出头部供事后排障
    records_dir = repo / ".project-evolution" / ".runtime" / "analyses"
    record_files = list(records_dir.glob("*.json"))
    assert len(record_files) == 1
    assert '"raw_head": "{\\"events\\": []}"' in record_files[0].read_text(encoding="utf-8")

    second = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert second.status == "existing" and second.noop is True
    assert len(calls) == 1


def test_duplicate_candidate_dropped_across_checkpoints(tmp_path: Path) -> None:
    """第二个 checkpoint 再次提出相同 parameter（同 before/after）被去重。"""
    repo, services, task, checkpoint1 = setup_task_with_change(tmp_path)
    calls: list[int] = []
    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=parameter_responder(calls)))
    analyzer.analyze_checkpoint(task.id, checkpoint1.id)

    # 新变更（不同 digest -> 不同 analysis_key），但模型重复输出同一事件
    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.3;\n"
        "    private const double OpacityMax = 0.95;\n"
        "}\n",
        encoding="utf-8",
    )
    checkpoint2 = services.checkpoints.create_checkpoint(task.id, summary="加 OpacityMax")
    outcome = analyzer.analyze_checkpoint(task.id, checkpoint2.id)

    assert outcome.status == "noop"  # 唯一候选被去重，无可提交事件
    assert outcome.deduped and "existing event" in outcome.deduped[0][1]
    events = list(iter_evolution_events(repo))
    assert len(events) == 1


def test_validator_rejection_diagnosed_not_committed(tmp_path: Path) -> None:
    """模型引用幻觉 Evidence ID -> Validator 拒绝并记录诊断，不写入账本。"""
    repo, _services, task, checkpoint = setup_task_with_change(tmp_path)

    def hallucinating(request) -> AnalyzerResponse:
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="幻觉事件",
                    evidence_ids=["EVD-does-not-exist"],
                    payload={"name": "X", "before": "1", "after": "2"},
                )
            ]
        )

    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=hallucinating))
    outcome = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert outcome.status == "noop"
    assert outcome.rejections[0].code == "EVIDENCE_UNKNOWN"
    assert not (repo / ".project-evolution" / "events" / "evolution.jsonl").exists()


def test_relevant_history_passed_to_provider(tmp_path: Path) -> None:
    """第二次分析时，第一次的已提交事件进入 relevant_history（去重上下文）。"""
    repo, services, task, checkpoint1 = setup_task_with_change(tmp_path)
    calls: list[int] = []
    seen: list[object] = []

    def respond(request) -> AnalyzerResponse:
        seen.append([h.event_id for h in request.relevant_history])
        return parameter_responder(calls)(request)

    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=respond))
    analyzer.analyze_checkpoint(task.id, checkpoint1.id)

    service_file = repo / "Core" / "WindowOpacityService.cs"
    service_file.write_text(
        "class WindowOpacityService {\n"
        "    private const double OpacityMin = 0.4;\n"
        "}\n",
        encoding="utf-8",
    )
    checkpoint2 = services.checkpoints.create_checkpoint(task.id, summary="0.3 -> 0.4")
    analyzer.analyze_checkpoint(task.id, checkpoint2.id)
    assert seen[0] == []
    assert len(seen[1]) == 1  # 第一轮的事件作为历史提供
