"""Evaluation Fixture Set 质量门槛测试（phase-05 §10/§12，05 §17）。

70 个离线样例（10 feature + 20 parameter + 10 decision + 10 bug_fix + 20 NOOP），
人工标注期望；OfflineRuleProvider + Validator 跑完整管线。

进入 Phase 06 前必须满足：

- Precision >= PRECISION_THRESHOLD（项目设定门槛）；
- NOOP fixture 零事件（假阳性直接污染演进历史）；
- 漏检率 <= MAX_MISS_RATE。
"""

from __future__ import annotations

import json
from pathlib import Path

from project_evolution.evolution.evaluation import (
    MAX_MISS_RATE,
    PRECISION_THRESHOLD,
    EvaluationFixture,
    OfflineRuleProvider,
    run_fixture_evaluation,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "evolution_fixtures.json"


def load_fixtures() -> list[EvaluationFixture]:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [EvaluationFixture.model_validate(item) for item in raw]


def test_fixture_set_composition() -> None:
    """构成与 05 §17 一致：10/20/10/10/20，共 70 个（50~70 区间的完整档）。"""
    fixtures = load_fixtures()
    assert len(fixtures) == 70
    counts = {"feature_change": 0, "parameter_change": 0, "decision": 0, "bug_fix": 0}
    noop = 0
    for fixture in fixtures:
        if fixture.is_noop:
            noop += 1
            continue
        for expectation in fixture.expected:
            counts[expectation.type] += 1
    assert counts == {
        "feature_change": 10,
        "parameter_change": 20,
        "decision": 10,
        "bug_fix": 10,
    }
    assert noop == 20


def test_fixture_ids_unique() -> None:
    fixtures = load_fixtures()
    ids = [fixture.id for fixture in fixtures]
    assert len(set(ids)) == len(ids)


def test_offline_precision_meets_threshold() -> None:
    """主验收门槛：Precision 达到项目设定值（phase-05 §12 最后一条）。"""
    report = run_fixture_evaluation(load_fixtures(), OfflineRuleProvider())
    detail = {
        "precision": report.precision,
        "recall": report.recall,
        "produced": report.produced_events,
        "matched": report.matched_events,
        "missed": report.missed_expected,
        "noop_violations": report.noop_violations,
        "false_parameters": report.false_parameters,
        "threshold": PRECISION_THRESHOLD,
    }
    print(json.dumps(detail, ensure_ascii=False))
    assert report.precision >= PRECISION_THRESHOLD, detail
    assert report.noop_violations == 0, detail
    assert (1 - report.recall) <= MAX_MISS_RATE + 1e-9, detail


def test_noop_fixtures_produce_no_events() -> None:
    fixtures = [f for f in load_fixtures() if f.is_noop]
    report = run_fixture_evaluation(fixtures, OfflineRuleProvider())
    assert report.produced_events == 0
    assert report.false_parameters == 0


def test_missed_and_rejected_are_reported_per_fixture() -> None:
    """报告粒度：每个 fixture 有 produced/matched/missed/rejected（可定位漏检）。"""
    report = run_fixture_evaluation(load_fixtures(), OfflineRuleProvider())
    assert len(report.per_fixture) == 70
    bad = [
        item
        for item in report.per_fixture
        if item.missed > 0 or item.rejected > 0
    ]
    print(json.dumps([item.model_dump() for item in bad], ensure_ascii=False, indent=2))
    # 允许个别漏检，但拒绝数必须为 0（规则模型输出不违反 Validator 约束）
    assert all(item.rejected == 0 for item in report.per_fixture)
