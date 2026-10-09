"""EvolutionAnalyzerService：Checkpoint -> 语义事件 的完整闭环（phase-05 §4/§5）。

```text
Checkpoint（Raw Ledger 重放）
-> ChangeDigest（快照增量重建 + evidence 清单 + 测试/agent 摘要）
-> Provider（模型候选事件）
-> Validator（Schema / Evidence / 幂等 / 字段约束）
-> Dedup（normalized key + exact before/after + title overlap）
-> evolution.jsonl（同一 analysis_batch_id 整批提交）
```

幂等（03-event-model.md §12 / 04 §7）：

- ``analysis_key = project_id | task_id | checkpoint_id | analyzer_version | digest_hash``
  （digest_hash 对除 ``created_at`` 外的 digest 内容取 sha256，保证重算稳定）；
- 同键已成功提交：返回已有 batch，不再 append，不再调用 Provider；
- NOOP 结果（events=[] 合法）记入 runtime 分析记录，避免重复调用 Provider。

分析失败（Provider 网络失败/超时/输出不可解析）转入 pending 队列
（phase-05 §9），Raw Ledger 不回滚。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from project_evolution.config import PEL_DIR_NAME, PelConfig, load_config
from project_evolution.errors import PelError, StorageError
from project_evolution.evolution.analyzer import (
    ANALYZER_VERSION,
    AnalyzerRequest,
    CandidateEvent,
    CheckpointSummary,
    EvolutionEventSummary,
    EvolutionModelProvider,
    ProjectSummary,
    TaskSummary,
)
from project_evolution.evolution.change_digest import (
    ChangeDigest,
    DigestAgentSummary,
    DigestEvidence,
    DigestTestResult,
)
from project_evolution.evolution.dedup import dedup_candidates
from project_evolution.evolution.pending import PendingAnalysis, PendingStore
from project_evolution.evolution.prompt import PROMPT_VERSION
from project_evolution.evolution.validator import (
    Rejection,
    ValidatorContext,
    validate_candidates,
)
from project_evolution.git.collector import build_digest_for_checkpoint
from project_evolution.ids import new_id
from project_evolution.ledger import EvolutionEventLedger
from project_evolution.models import EvolutionEvent, RawEvent
from project_evolution.tasks.store import TaskStore

_LOG = logging.getLogger(__name__)

#: 提供给模型的近期事件数上限（05 §5 输入预算 max_history_events）
MAX_HISTORY_EVENTS = 10

AnalysisStatus = Literal["committed", "noop", "existing"]


class AnalysisError(PelError):
    """分析执行失败（Provider/存储）。失败细节见 pending 记录。"""


class AnalysisOutcome(BaseModel):
    """一次分析（或幂等重放）的结果。"""

    analysis_id: str
    analysis_key: str
    status: AnalysisStatus
    events: list[EvolutionEvent] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)
    #: 去重丢弃的候选（index, reason）
    deduped: list[tuple[int, str]] = Field(default_factory=list)
    provider: str = ""
    noop: bool = False


class AnalysisRecord(BaseModel):
    """runtime 分析记录（可重建缓存；Canonical 幂等以 evolution.jsonl 为准）。"""

    analysis_id: str
    analysis_key: str
    status: AnalysisStatus
    event_ids: list[str] = Field(default_factory=list)
    provider: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    #: 模型原始输出头部（诊断用）。NOOP 无事件落账，事后排障只能靠它；
    #: 旧记录无此字段，读取时按空串兼容。
    raw_head: str = ""


class PendingRetryResult(BaseModel):
    """retry_pending() 的单项结果：成功带 outcome，失败带 error。"""

    pending: PendingAnalysis
    outcome: AnalysisOutcome | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


class AnalysisRecordStore:
    """.runtime/analyses/<key-hash>.json：NOOP 结果的幂等缓存。"""

    def __init__(self, project_root: str | Path, *, runtime_dir: str = ".runtime"):
        self._dir = Path(project_root) / PEL_DIR_NAME / runtime_dir / "analyses"

    def _path(self, analysis_key: str) -> Path:
        digest = hashlib.sha256(analysis_key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def find(self, analysis_key: str) -> AnalysisRecord | None:
        path = self._path(analysis_key)
        if not path.is_file():
            return None
        try:
            return AnalysisRecord.model_validate(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError) as exc:
            _LOG.warning("ignoring corrupt analysis record %s: %s", path, exc)
            return None

    def save(self, record: AnalysisRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record.analysis_key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def digest_hash(digest: ChangeDigest) -> str:
    """对除 ``created_at`` 外的 digest 内容取稳定 sha256（重算不漂移）。"""
    data = digest.model_dump(mode="json", exclude={"created_at"})
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_analysis_key(
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    analyzer_version: str,
    digest: ChangeDigest,
) -> str:
    return "|".join(
        [project_id, task_id, checkpoint_id, analyzer_version, digest_hash(digest)]
    )


class EvolutionAnalyzerService:
    """把某个 Checkpoint 的事实转换为语义事件并提交 evolution.jsonl。"""

    def __init__(
        self,
        project_root: str | Path,
        provider: EvolutionModelProvider,
        *,
        config: PelConfig | None = None,
        task_store: TaskStore | None = None,
        evolution_ledger: EvolutionEventLedger | None = None,
    ):
        self._root = Path(project_root)
        self._config = config if config is not None else load_config(self._root)
        self._store = (
            task_store
            if task_store is not None
            else TaskStore(self._root, config=self._config)
        )
        self.ledger = (
            evolution_ledger
            if evolution_ledger is not None
            else EvolutionEventLedger(
                self._root,
                events_path=(
                    self._root / PEL_DIR_NAME / self._config.events_dir / "evolution.jsonl"
                ),
            )
        )
        self._records = AnalysisRecordStore(self._root, runtime_dir=self._config.runtime_dir)
        self.pending = PendingStore(self._root, runtime_dir=self._config.runtime_dir)
        self.provider = provider

    # ---------------------------------------------------------------- public

    def analyze_checkpoint(self, task_id: str, checkpoint_id: str) -> AnalysisOutcome:
        """同步执行完整闭环（CLI / 测试场景；默认产品路径是后台异步 worker）。"""
        return asyncio.run(self.analyze_checkpoint_async(task_id, checkpoint_id))

    def retry_pending(self) -> list[PendingRetryResult]:
        """重试全部待重试分析（phase-05 §9）；成功清除记录，失败保留并累加 attempts。"""
        results: list[PendingRetryResult] = []
        for record in self.pending.list_pending():
            try:
                outcome = self.analyze_checkpoint(record.task_id, record.checkpoint_id)
            except PelError as exc:
                results.append(
                    PendingRetryResult(
                        pending=record,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            results.append(PendingRetryResult(pending=record, outcome=outcome))
        return results

    async def analyze_checkpoint_async(
        self, task_id: str, checkpoint_id: str
    ) -> AnalysisOutcome:
        request, context = self._prepare(task_id, checkpoint_id)
        analysis_key = context.analysis_key

        existing = self.ledger.find_batch(analysis_key)
        if existing:
            self.pending.remove_for_checkpoint(task_id, checkpoint_id)
            return AnalysisOutcome(
                analysis_id=existing[0].analysis_batch_id,
                analysis_key=analysis_key,
                status="existing",
                events=existing,
                provider=getattr(self.provider, "name", ""),
            )
        cached = self._records.find(analysis_key)
        if cached is not None and cached.status == "noop":
            self.pending.remove_for_checkpoint(task_id, checkpoint_id)
            return AnalysisOutcome(
                analysis_id=cached.analysis_id,
                analysis_key=analysis_key,
                status="existing",
                provider=cached.provider,
                noop=True,
            )

        analysis_id = new_id("ANL")
        try:
            response = await self.provider.analyze(request)
        except PelError as exc:
            # 模型失败：事实与证据已在 Raw Ledger，语义分析转 pending（§9）
            self._save_pending(
                analysis_id=analysis_id,
                analysis_key=analysis_key,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                error=f"{type(exc).__name__}: {exc}",
                kind=getattr(exc, "kind", "error"),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - Provider 实现的意外异常统一进 pending
            provider_name = getattr(self.provider, "name", "?")
            self._save_pending(
                analysis_id=analysis_id,
                analysis_key=analysis_key,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                error=f"{type(exc).__name__}: {exc}",
                kind="error",
            )
            raise AnalysisError(f"provider {provider_name} failed: {exc}") from exc

        validated = validate_candidates(response.events, context)
        deduped = dedup_candidates(
            validated.accepted, self._relevant_existing_events(task_id)
        )
        now = datetime.now(timezone.utc)
        events = [
            self._to_evolution_event(
                candidate,
                request=request,
                analysis_id=analysis_id,
                analysis_key=analysis_key,
                timestamp=now,
            )
            for candidate in deduped.kept
        ]
        if events:
            try:
                events = self.ledger.append_many(events)
            except PelError:
                raise
            except OSError as exc:
                raise StorageError(f"cannot append evolution events: {exc}") from exc
        outcome = AnalysisOutcome(
            analysis_id=analysis_id,
            analysis_key=analysis_key,
            status="noop" if not events else "committed",
            events=events,
            rejections=validated.rejections,
            deduped=deduped.duplicates,
            provider=getattr(response, "model", None) or getattr(self.provider, "name", ""),
            noop=not events,
        )
        self._records.save(
            AnalysisRecord(
                analysis_id=analysis_id,
                analysis_key=analysis_key,
                status=outcome.status,
                event_ids=[event.id for event in events],
                provider=outcome.provider,
                raw_head=(getattr(response, "raw_output", "") or "")[:2000],
            )
        )
        self.pending.remove_for_checkpoint(task_id, checkpoint_id)
        return outcome

    # ---------------------------------------------------------------- inside

    def _save_pending(
        self,
        *,
        analysis_id: str,
        analysis_key: str,
        task_id: str,
        checkpoint_id: str,
        error: str,
        kind: str,
    ) -> None:
        """失败入 pending：同 analysis_key 的重复失败更新原记录（attempts 递增）。"""
        record = self.pending.find_by_key(analysis_key)
        if record is None:
            record = PendingAnalysis(
                analysis_id=analysis_id,
                analysis_key=analysis_key,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                provider=getattr(self.provider, "name", "?"),
            )
        record = record.model_copy(
            update={
                "last_error": error,
                "last_error_kind": kind,
                "provider": getattr(self.provider, "name", record.provider),
                "task_id": task_id,
                "checkpoint_id": checkpoint_id,
            }
        )
        self.pending.save(record)

    def _prepare(
        self, task_id: str, checkpoint_id: str
    ) -> tuple[AnalyzerRequest, ValidatorContext]:
        """从 Raw Ledger 重建请求与校验上下文（不依赖任何内存状态）。"""
        replay = self._store.replay()
        checkpoint_event: RawEvent | None = None
        task_title = ""
        agent_summaries: list[DigestAgentSummary] = []
        test_results: list[DigestTestResult] = []
        evidence_entries: dict[str, DigestEvidence] = {}
        known_ids: set[str] = set()

        for event in replay:
            payload = event.payload if isinstance(event.payload, dict) else {}
            for eid in (payload.get("diff_evidence_id"), payload.get("evidence_id")):
                if isinstance(eid, str) and eid:
                    known_ids.add(eid)
            if event.kind == "task_started" and event.task_id == task_id:
                title = payload.get("title")
                task_title = title if isinstance(title, str) else task_title
            if event.checkpoint_id != checkpoint_id:
                continue
            if event.kind == "checkpoint_created":
                checkpoint_event = event
                eid = payload.get("diff_evidence_id")
                if isinstance(eid, str) and eid:
                    evidence_entries.setdefault(
                        eid, DigestEvidence(id=eid, type="git_diff", what="checkpoint diff")
                    )
            elif event.kind in ("test_passed", "test_failed"):
                command = str(payload.get("command") or "")
                if command:
                    test_results.append(
                        DigestTestResult(
                            command=command,
                            status="passed" if event.kind == "test_passed" else "failed",
                            source=str(payload.get("source") or "agent_claim"),
                            evidence_id=event.id,
                        )
                    )
                    evidence_entries.setdefault(
                        event.id,
                        DigestEvidence(id=event.id, type="test_result", what=command),
                    )
            elif event.kind == "agent_summary":
                text = payload.get("summary")
                if isinstance(text, str) and text:
                    agent = payload.get("source_agent")
                    agent_summaries.append(
                        DigestAgentSummary(
                            text=text,
                            source_agent=str(agent) if agent else None,
                            evidence_id=event.id,
                        )
                    )
                    evidence_entries.setdefault(
                        event.id,
                        DigestEvidence(id=event.id, type="raw_event", what="agent_summary"),
                    )

        if checkpoint_event is None:
            raise AnalysisError(f"checkpoint {checkpoint_id} not found in raw ledger")
        project_id = checkpoint_event.project_id

        digest = build_digest_for_checkpoint(
            self._root,
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
        )
        digest.evidence = [evidence_entries[key] for key in sorted(evidence_entries)]
        digest.test_results = test_results
        digest.agent_summaries = agent_summaries

        payload = checkpoint_event.payload
        request = AnalyzerRequest(
            project=ProjectSummary(project_id=project_id, name=self._root.name),
            task=TaskSummary(task_id=task_id, title=task_title),
            checkpoint=CheckpointSummary(
                checkpoint_id=checkpoint_id,
                index=int(payload.get("index") or 0),
                reason=payload.get("reason"),
                summary=payload.get("summary"),
            ),
            digest=digest,
            relevant_history=self._relevant_history(task_id),
        )
        context = ValidatorContext(
            digest_evidence={entry.id: entry.type for entry in digest.evidence},
            known_evidence_ids=known_ids,
            analysis_key=compute_analysis_key(
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                analyzer_version=ANALYZER_VERSION,
                digest=digest,
            ),
            existing_analysis_keys=self.ledger.analysis_keys(),
        )
        return request, context

    def _relevant_existing_events(self, task_id: str) -> list[EvolutionEvent]:
        return [
            event for event in self.ledger.iter_events() if event.task_id == task_id
        ]

    def _relevant_history(self, task_id: str) -> list[EvolutionEventSummary]:
        events = self._relevant_existing_events(task_id)[-MAX_HISTORY_EVENTS:]
        return [
            EvolutionEventSummary(
                event_id=event.id,
                type=event.type,
                title=event.title,
                detail=_summarize_payload(event),
            )
            for event in reversed(events)  # 最近的在前
        ]

    @staticmethod
    def _to_evolution_event(
        candidate: CandidateEvent,
        *,
        request: AnalyzerRequest,
        analysis_id: str,
        analysis_key: str,
        timestamp: datetime,
    ) -> EvolutionEvent:
        return EvolutionEvent(
            id=new_id("EVO"),
            project_id=request.project.project_id,
            task_id=request.task.task_id,
            checkpoint_id=request.checkpoint.checkpoint_id,
            timestamp=timestamp,
            type=candidate.type,  # type: ignore[arg-type]  # Validator 已限制在四种之内
            title=candidate.title.strip(),
            description=candidate.description,
            evidence_ids=list(candidate.evidence_ids),
            confidence=candidate.confidence,
            analyzer_version=ANALYZER_VERSION,
            prompt_version=PROMPT_VERSION,
            analysis_batch_id=analysis_id,
            analysis_key=analysis_key,
            payload=dict(candidate.payload),
        )


def _summarize_payload(event: EvolutionEvent) -> str:
    payload: dict[str, Any] = event.payload if isinstance(event.payload, dict) else {}
    keys = {
        "parameter_change": ("name", "before", "after"),
        "feature_change": ("feature", "action"),
        "decision": ("subject", "decision"),
        "bug_fix": ("problem", "fix"),
    }
    parts = [
        f"{key}={payload[key]}" for key in keys.get(event.type, ()) if key in payload
    ]
    return "; ".join(parts)
