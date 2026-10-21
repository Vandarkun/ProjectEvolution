"""向量检索（retrieval extra）：机制用确定性桩，语义用真实模型 opt-in。

机制测试覆盖：召回排序、缓存（二次查询不再嵌入）、换模型清旧向量、
空查询、检索不可用的报错路径。真实模型测试验证跨语言召回
（中文查询命中英文事件——phase-09 实测的精确匹配盲区）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.gitrepo import init_repo

from project_evolution.evolution import (
    AnalyzerResponse,
    CandidateEvent,
    EvolutionAnalyzerService,
    MockProvider,
)
from project_evolution.git.collector import create_git_aware_task_services
from project_evolution.index.query import QueryService
from project_evolution.retrieval.embedder import (
    Embedder,
    HashingEmbedder,
    RetrievalError,
    default_embedder,
)


class _CountingEmbedder:
    """包装桩：统计 embed 调用，验证缓存命中。"""

    def __init__(self, inner: Embedder):
        self.inner = inner
        self.calls = 0

    @property
    def name(self) -> str:
        return self.inner.name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return self.inner.embed(texts)


def _make_events(repo: Path) -> None:
    """写入两条语义迥异的事件：参数调整 + 英文决策（跨语言目标）。"""
    services = create_git_aware_task_services(repo, project_id="PRJ-vec")
    task = services.tasks.start_task("PRJ-vec", "向量检索测试", agent="test")
    repo_file = repo / "Core" / "WindowOpacityService.cs"
    repo_file.parent.mkdir(exist_ok=True)
    repo_file.write_text("class A { const double X = 1; }", encoding="utf-8")

    def respond(request) -> AnalyzerResponse:
        assert request.digest.evidence, "digest must carry evidence manifest"
        return AnalyzerResponse(
            events=[
                CandidateEvent(
                    type="parameter_change",
                    title="滚轮透明度步长 10% -> 5%",
                    description="透明度步进参数调整，OpacityMath 统一",
                    evidence_ids=[request.digest.evidence[0].id],
                    payload={"name": "OpacityMath.Step", "before": "0.1", "after": "0.05"},
                ),
                CandidateEvent(
                    type="decision",
                    title="QR decode attempted before OCR for captured content",
                    description="try QR decoding first, fall back to OCR",
                    evidence_ids=[request.digest.evidence[0].id],
                    payload={
                        "subject": "content extraction priority",
                        "decision": "QR before OCR",
                        "reason": "deterministic decode wins",
                        "alternatives": ["OCR first"],
                        "supersedes": [],
                    },
                ),
            ]
        )

    checkpoint = services.checkpoints.create_checkpoint(
        task.id,
        summary="test change",
        agent_summary="透明度参数调整，取字链路 QR 优先",
        tests=[{"command": "dotnet test", "status": "passed", "source": "agent_claim"}],
    )
    analyzer = EvolutionAnalyzerService(repo, MockProvider(responder=respond))
    outcome = analyzer.analyze_checkpoint(task.id, checkpoint.id)
    assert outcome.status == "committed", [str(r) for r in outcome.rejections]
    from project_evolution.index import update_projection

    update_projection(repo)


@pytest.fixture()
def vec_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path)
    _make_events(repo)
    return repo


def _query_with(repo: Path, embedder: Embedder) -> QueryService:
    import project_evolution.index.query as query_mod

    service = QueryService(repo)
    original = query_mod._search_embedder
    query_mod._search_embedder = lambda: embedder
    try:
        yield service
    finally:
        query_mod._search_embedder = original


def test_recall_ranks_by_token_overlap(vec_repo: Path) -> None:
    """桩向量按字符 trigram 重叠排序：中文查询应命中中文参数事件。"""
    counting = _CountingEmbedder(HashingEmbedder())
    for service in _query_with(vec_repo, counting):
        hits = service.search_events("滚轮透明度步长调整", limit=2)
        assert hits, "应至少召回一条"
        assert hits[0].type == "parameter_change"
        assert "透明度" in hits[0].title
        assert 0.0 < hits[0].score <= 1.0
        assert hits[0].evidence_ids  # 引用带证据，可下钻
        # 第二次查询命中缓存：不再嵌入事件（仅嵌入查询本身）
        first_calls = counting.calls
        service.search_events("透明度", limit=2)
        assert counting.calls == first_calls + 1


def test_model_switch_purges_stale_vectors(vec_repo: Path) -> None:
    """换 embedder（模型版本变化）后旧向量被清理，结果仍正确。"""
    import sqlite3

    for service in _query_with(vec_repo, HashingEmbedder(name="hash-a")):
        service.search_events("透明度")
    for service in _query_with(vec_repo, HashingEmbedder(name="hash-b")):
        hits = service.search_events("透明度")
        assert hits
    con = sqlite3.connect(service.store.connection() is not None and ":memory:")
    con.close()
    con = service.store.connection()
    try:
        models = {str(r[0]) for r in con.execute("SELECT DISTINCT model_id FROM event_vectors")}
    finally:
        con.close()
    assert models == {"hash-b"}, f"旧模型向量应被清理: {models}"


def test_empty_query_returns_empty(vec_repo: Path) -> None:
    for service in _query_with(vec_repo, HashingEmbedder()):
        assert service.search_events("   ") == []


def test_missing_dependency_raises_retrieval_error(vec_repo: Path, monkeypatch) -> None:
    """fastembed 缺失时给出可操作的安装提示，而不是裸 ImportError。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    embedder = default_embedder()
    with pytest.raises(RetrievalError) as excinfo:
        embedder.embed(["x"])
    assert "retrieval" in str(excinfo.value)


def test_index_level_determinism() -> None:
    """同一文本两次嵌入结果一致（重建投影后向量可复现）。"""
    emb = HashingEmbedder()
    a, b = emb.embed(["透明度 步长"]), emb.embed(["透明度 步长"])
    assert a == b


# ------------------------------------------------- 真实模型（opt-out skip）

fastembed_missing = pytest.importorskip.reason if False else None
try:
    import fastembed  # noqa: F401

    _HAS_FASTEMBED = True
except ImportError:  # pragma: no cover
    _HAS_FASTEMBED = False


@pytest.mark.skipif(not _HAS_FASTEMBED, reason="fastembed not installed")
def test_real_model_cross_language_recall(vec_repo: Path) -> None:
    """phase-09 的精确匹配盲区：中文查询命中英文事件，反之亦然。"""
    from project_evolution.retrieval.embedder import FastEmbedEmbedder

    for service in _query_with(vec_repo, FastEmbedEmbedder()):
        hits = service.search_events("取内容时二维码和文字识别的先后顺序", limit=2)
        assert hits, "跨语言召回不应为空"
        top = hits[0]
        assert top.type == "decision"
        assert "QR" in top.title or "OCR" in top.title

        hits_en = service.search_events("why was wheel opacity step changed", limit=2)
        assert hits_en
        assert any("透明度" in h.title for h in hits_en), "英文查询也应命中中文事件"
