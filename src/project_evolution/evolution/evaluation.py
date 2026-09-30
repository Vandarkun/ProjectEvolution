"""离线评估：Evaluation Fixture Set 与 Precision 门槛（phase-05 §10，05 §17）。

两个角色：

- :class:`OfflineRuleProvider`：确定性规则"模型"（MockProvider 的 responder 模式），
  在离线 fixture 上代替真实 LLM 执行提取。它是评估与 CI 的基线，不是产品路径；
- :func:`run_fixture_evaluation`：跑完整管线（provider -> validator），
  按人工标注计算 Precision / 漏检 / 假 Parameter / NOOP 违例。

门槛（"项目设定门槛"，进入 Phase 06 前必须达到）：
:data:`PRECISION_THRESHOLD`，且 NOOP fixture 不得产生任何事件
（宁可漏掉，不要污染演进历史，05 §17）。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from project_evolution.evolution.analyzer import (
    AnalyzerRequest,
    AnalyzerResponse,
    CandidateEvent,
)
from project_evolution.evolution.change_digest import (
    ChangeDigest,
    DigestAgentSummary,
    DigestEvidence,
    DigestTestResult,
    SelectedHunk,
)
from project_evolution.evolution.validator import (
    ValidatorContext,
    validate_candidates,
)

#: 项目设定的 Precision 门槛（phase-05 §12：达到后才进入 Phase 06）
PRECISION_THRESHOLD = 0.85
#: 漏检率上限（报告 + 辅助门槛；Precision 仍是主门槛）
MAX_MISS_RATE = 0.25

_ASSIGN_RE = re.compile(
    r"^\s*(?:(?:private|public|protected|internal|static|final|readonly|const|let|var|export|declare)\s+)*"
    r"(?:[A-Za-z_][\w.]*\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(:=|=|:)\s*(.+?)\s*;?\s*$"
)
_CONST_KEYWORDS = ("const", "static final", "readonly", "final ")
_CONFIG_SUFFIXES = (
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".json", ".xml",
)
_TEST_MARKERS = ("test", "spec")
_DECISION_MARKERS = ("决策:", "决定:", "decision:")
_FEATURE_MARKERS = ("新增能力:", "能力:", "feature:")
_PROBLEM_MARKERS = ("问题:", "problem:")
_CAUSE_MARKERS = ("原因:", "cause:")
_FIX_MARKERS = ("修复:", "fix:")
_ALTERNATIVE_RE = re.compile(r"(?:而不是|instead of)\s*(.+)$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _is_test_path(path: str) -> bool:
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return any(marker in stem for marker in _TEST_MARKERS)


def _is_config_path(path: str) -> bool:
    return path.replace("\\", "/").lower().endswith(_CONFIG_SUFFIXES)


class FixtureExpectation(BaseModel):
    """人工标注的一条期望事件（05 §10：类型 / before/after / 证据来源）。"""

    type: str
    name: str | None = None  # parameter
    feature: str | None = None  # feature_change
    subject: str | None = None  # decision
    problem: str | None = None  # bug_fix
    before: str | None = None
    after: str | None = None


class EvaluationFixture(BaseModel):
    """一个离线样例：变更事实 + 人工标注期望（expected 为空即 NOOP 样例）。"""

    id: str
    task_title: str
    checkpoint_summary: str = ""
    hunks: list[dict[str, str]] = Field(default_factory=list)  # path/header/content
    added_files: list[str] = Field(default_factory=list)
    agent_summaries: list[str] = Field(default_factory=list)
    test_results: list[dict[str, str]] = Field(default_factory=list)
    expected: list[FixtureExpectation] = Field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.expected


class FixtureEvaluation(BaseModel):
    fixture_id: str
    produced: int
    matched: int
    missed: int
    rejected: int
    produced_types: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    total_fixtures: int
    noop_fixtures: int
    produced_events: int
    matched_events: int
    missed_expected: int
    noop_violations: int  # NOOP fixture 上产生的事件数（假阳性，直接污染）
    false_parameters: int  # NOOP fixture 上产生的 parameter_change 数
    precision: float
    recall: float
    per_fixture: list[FixtureEvaluation] = Field(default_factory=list)

    @property
    def passes(self) -> bool:
        return (
            self.precision >= PRECISION_THRESHOLD
            and self.noop_violations == 0
            and (1 - self.recall) <= MAX_MISS_RATE + 1e-9
        )


# ------------------------------------------------------- offline rule model


class OfflineRuleProvider:
    """确定性规则提取器（评估基线，MockProvider responder 形态）。

    规则刻意保守（宁漏勿错）：

    - parameter_change：同一 hunk 内同名赋值 ``-old -> +new``，
      且名字是 UPPER_SNAKE / 行内带 const 族关键字 / 配置文件路径；
      测试文件中的变化不提取；
    - decision：agent summary 带 决策/决定/decision 标记；
    - bug_fix：summary 带 问题/problem + 修复/fix 标记（fix 必须存在）；
    - feature_change：summary 带 新增能力/feature 标记，或有新增非测试源文件
      且 summary 含能力语义词；
    - 其余 -> events=[]（NOOP）。
    """

    name = "offline-rules"

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerResponse:
        events: list[CandidateEvent] = []
        digest = request.digest
        summary_evidence = {
            summary.evidence_id: summary.text
            for summary in digest.agent_summaries
            if summary.evidence_id
        }

        events.extend(self._parameters(digest))
        events.extend(self._decisions(request, summary_evidence))
        events.extend(self._bug_fixes(request, summary_evidence))
        events.extend(self._features(request, summary_evidence))
        return AnalyzerResponse(events=events, model=self.name)

    # ---------------------------------------------------------------- inside

    def _parameters(self, digest: ChangeDigest) -> list[CandidateEvent]:
        events: list[CandidateEvent] = []
        diff_evidence = {
            entry.what: entry.id
            for entry in digest.evidence
            if entry.type == "git_diff"
        }
        for hunk in digest.selected_hunks:
            if _is_test_path(hunk.path):
                continue
            evidence_id = diff_evidence.get(hunk.path)
            if evidence_id is None:
                continue
            removed: dict[str, str] = {}
            added: dict[str, str] = {}
            for line in hunk.content.splitlines():
                sign = line[:1]
                body = line[1:]
                match = _ASSIGN_RE.match(body)
                if not match:
                    continue
                name, value = match.group(1), match.group(3)
                if not self._looks_like_parameter(name, body, hunk.path):
                    continue
                target = removed if sign == "-" else added if sign == "+" else None
                if target is not None and name not in target:
                    target[name] = _strip_quotes(value)
            for name in sorted(set(removed) & set(added)):
                if removed[name] == added[name]:
                    continue
                events.append(
                    CandidateEvent(
                        type="parameter_change",
                        title=f"{name} {removed[name]} -> {added[name]}",
                        description=f"{hunk.path}: {name} changed",
                        evidence_ids=[evidence_id],
                        payload={
                            "name": name,
                            "before": removed[name],
                            "after": added[name],
                            "reason": None,
                        },
                    )
                )
        return events

    @staticmethod
    def _looks_like_parameter(name: str, line: str, path: str) -> bool:
        if _is_config_path(path):
            return True
        lowered = f" {line.lower()} "
        if any(keyword in lowered for keyword in _CONST_KEYWORDS):
            return True
        return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name))

    @staticmethod
    def _decisions(
        request: AnalyzerRequest, summary_evidence: dict[str, str]
    ) -> list[CandidateEvent]:
        events: list[CandidateEvent] = []
        for evidence_id, text in summary_evidence.items():
            for marker in _DECISION_MARKERS:
                pos = text.find(marker)
                if pos == -1:
                    continue
                body = text[pos + len(marker) :].strip()
                alternatives: list[str] = []
                alt = _ALTERNATIVE_RE.search(body)
                if alt:
                    alternatives.append(alt.group(1).strip("。.;；"))
                    body = body[: alt.start()].strip("，, ")
                events.append(
                    CandidateEvent(
                        type="decision",
                        title=f"{request.task.title}: {body[:60]}",
                        description=text,
                        evidence_ids=[evidence_id],
                        payload={
                            "subject": request.task.title,
                            "decision": body,
                            "reason": None,
                            "alternatives": alternatives,
                        },
                    )
                )
                break
        return events

    @staticmethod
    def _bug_fixes(
        request: AnalyzerRequest, summary_evidence: dict[str, str]
    ) -> list[CandidateEvent]:
        events: list[CandidateEvent] = []
        diff_evidence = first_diff_evidence(request.digest)
        for evidence_id, text in summary_evidence.items():
            problem = _extract_after(text, _PROBLEM_MARKERS)
            fix = _extract_after(text, _FIX_MARKERS)
            if not problem or not fix:
                continue
            cause = _extract_after(text, _CAUSE_MARKERS)
            verification = (
                f"{request.digest.test_results[0].command} passed"
                if any(t.status == "passed" for t in request.digest.test_results)
                else None
            )
            events.append(
                CandidateEvent(
                    type="bug_fix",
                    title=f"修复: {problem[:60]}",
                    description=text,
                    evidence_ids=[evidence_id] + ([diff_evidence] if diff_evidence else []),
                    payload={
                        "problem": problem,
                        "cause": cause,
                        "fix": fix,
                        "verification": verification,
                    },
                )
            )
        return events

    @staticmethod
    def _features(
        request: AnalyzerRequest, summary_evidence: dict[str, str]
    ) -> list[CandidateEvent]:
        events: list[CandidateEvent] = []
        for evidence_id, text in summary_evidence.items():
            for marker in _FEATURE_MARKERS:
                pos = text.find(marker)
                if pos == -1:
                    continue
                feature = text[pos + len(marker) :].split(";")[0].split("。")[0].strip()
                events.append(
                    CandidateEvent(
                        type="feature_change",
                        title=f"新增能力: {feature}",
                        description=text,
                        evidence_ids=[evidence_id],
                        payload={"action": "added", "feature": feature},
                    )
                )
                break
        if events:
            return events
        # 无标记时的保守推断：新增非测试源文件 + summary 含能力语义词
        capability_words = ("新增", "添加", "支持", "实现", "add", "support", "implement")
        summary_all = " ".join(summary_evidence.values()).lower()
        if not any(word in summary_all for word in capability_words):
            return []
        diff_evidence = {
            entry.what: entry.id for entry in request.digest.evidence if entry.type == "git_diff"
        }
        for path in request.digest.added_files:
            if _is_test_path(path) or _is_config_path(path):
                continue
            events.append(
                CandidateEvent(
                    type="feature_change",
                    title=f"新增模块: {path}",
                    description=request.checkpoint.summary or "",
                    evidence_ids=[diff_evidence[path]],
                    payload={
                        "action": "added",
                        "feature": path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    },
                )
            )
        return events


# ------------------------------------------------------------------- helpers


def digest_agent_texts(digest: ChangeDigest) -> list[str]:
    return [summary.text for summary in digest.agent_summaries]


def first_diff_evidence(digest: ChangeDigest) -> str | None:
    for entry in digest.evidence:
        if entry.type == "git_diff":
            return entry.id
    return None


def _extract_after(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        pos = text.find(marker)
        if pos != -1:
            tail = text[pos + len(marker) :].strip()
            # 截到下一个 标记 之前（用分号或句号分隔的字段）
            tail = re.split(r"[;；]", tail)[0].strip("。，, ")
            return tail or None
    return None


# ---------------------------------------------------------------- evaluation


def fixture_to_request(fixture: EvaluationFixture) -> tuple[AnalyzerRequest, ValidatorContext]:
    """把离线样例组装成完整管线输入（evidence 清单按摘要/hunk 生成）。"""
    evidence: list[DigestEvidence] = []
    agent_summaries: list[DigestAgentSummary] = []
    for i, text in enumerate(fixture.agent_summaries, start=1):
        eid = f"EVD-S{i}"
        agent_summaries.append(DigestAgentSummary(text=text, evidence_id=eid))
        evidence.append(DigestEvidence(id=eid, type="raw_event", what="agent_summary"))
    test_results: list[DigestTestResult] = []
    for i, item in enumerate(fixture.test_results, start=1):
        eid = f"EVD-T{i}"
        test_results.append(
            DigestTestResult(
                command=item.get("command", "test"),
                status=item.get("status", "passed"),
                source=item.get("source", "agent_claim"),
                evidence_id=eid,
            )
        )
        evidence.append(
            DigestEvidence(id=eid, type="test_result", what=item.get("command", "test"))
        )
    hunks: list[SelectedHunk] = []
    for i, hunk in enumerate(fixture.hunks, start=1):
        eid = f"EVD-D{i}"
        hunks.append(
            SelectedHunk(
                path=hunk["path"], header=hunk.get("header", "@@"), content=hunk["content"]
            )
        )
        evidence.append(DigestEvidence(id=eid, type="git_diff", what=hunk["path"]))

    changed = [h["path"] for h in fixture.hunks]
    digest = ChangeDigest(
        project_id="PRJ-eval",
        task_id="TASK-eval",
        checkpoint_id="CHK-eval",
        changed_files=sorted(set(changed) | set(fixture.added_files)),
        added_files=list(fixture.added_files),
        selected_hunks=hunks,
        evidence=evidence,
        test_results=test_results,
        agent_summaries=agent_summaries,
        summary=fixture.checkpoint_summary,
    )
    request = AnalyzerRequest.model_validate(
        {
            "project": {"project_id": "PRJ-eval", "name": "eval"},
            "task": {"task_id": "TASK-eval", "title": fixture.task_title},
            "checkpoint": {
                "checkpoint_id": "CHK-eval",
                "index": 1,
                "summary": fixture.checkpoint_summary,
            },
            "digest": digest.model_dump(mode="json"),
        }
    )
    context = ValidatorContext(
        digest_evidence={entry.id: entry.type for entry in evidence},
        known_evidence_ids={entry.id for entry in evidence},
        analysis_key=f"eval|{fixture.id}",
        existing_analysis_keys=set(),
    )
    return request, context


def _identity_of(event: Any) -> tuple:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.type == "parameter_change":
        return ("parameter_change", str(payload.get("name")))
    if event.type == "feature_change":
        return ("feature_change", str(payload.get("feature")))
    if event.type == "decision":
        return ("decision", str(payload.get("subject")))
    if event.type == "bug_fix":
        return ("bug_fix", str(payload.get("problem")))
    return (event.type, "")


def _values_of(event: Any) -> tuple:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (payload.get("before"), payload.get("after"))


def _clean_values(values: Any) -> tuple:
    """把 (before, after) 规整为非 None 字符串元组，供期望比对。"""
    if values is None:
        return ()
    return tuple(str(v) for v in values if v is not None)


def run_fixture_evaluation(
    fixtures: list[EvaluationFixture], provider: Any | None = None
) -> EvaluationReport:
    """对全部 fixture 跑 provider -> validator，按标注计算报告。"""
    import asyncio

    provider = provider or OfflineRuleProvider()
    per_fixture: list[FixtureEvaluation] = []
    produced_total = matched_total = missed_total = noop_violations = false_params = 0
    noop_count = 0

    for fixture in fixtures:
        request, context = fixture_to_request(fixture)
        response = asyncio.run(provider.analyze(request))
        outcome = validate_candidates(response.events, context)
        events = outcome.accepted

        expected_ids = [_expected_identity(exp) for exp in fixture.expected]
        expected_values = [
            (exp.before, exp.after) if exp.type == "parameter_change" else None
            for exp in fixture.expected
        ]
        used: set[int] = set()
        matched = 0
        for event in events:
            identity = _identity_of(event)
            values = _values_of(event)
            pairs = list(zip(expected_ids, expected_values, strict=True))
            for index, (exp_id, exp_values) in enumerate(pairs):
                if index in used or identity != exp_id:
                    continue
                expected_tuple = _clean_values(exp_values)
                produced_tuple = _clean_values(values)
                if exp_values is not None and expected_tuple != produced_tuple:
                    continue
                used.add(index)
                matched += 1
                break
        produced_total += len(events)
        matched_total += matched
        missed = len(fixture.expected) - matched
        missed_total += missed
        if fixture.is_noop:
            noop_count += 1
            noop_violations += len(events)
            false_params += sum(1 for e in events if e.type == "parameter_change")
        per_fixture.append(
            FixtureEvaluation(
                fixture_id=fixture.id,
                produced=len(events),
                matched=matched,
                missed=missed,
                rejected=len(outcome.rejections),
                produced_types=[e.type for e in events],
            )
        )

    precision = matched_total / produced_total if produced_total else 1.0
    total_expected = matched_total + missed_total
    recall = matched_total / total_expected if total_expected else 1.0
    return EvaluationReport(
        total_fixtures=len(fixtures),
        noop_fixtures=noop_count,
        produced_events=produced_total,
        matched_events=matched_total,
        missed_expected=missed_total,
        noop_violations=noop_violations,
        false_parameters=false_params,
        precision=round(precision, 4),
        recall=round(recall, 4),
        per_fixture=per_fixture,
    )


def _expected_identity(exp: FixtureExpectation) -> tuple:
    if exp.type == "parameter_change":
        return ("parameter_change", str(exp.name))
    if exp.type == "feature_change":
        return ("feature_change", str(exp.feature))
    if exp.type == "decision":
        return ("decision", str(exp.subject))
    if exp.type == "bug_fix":
        return ("bug_fix", str(exp.problem))
    return (exp.type, "")
