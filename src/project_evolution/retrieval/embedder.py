"""Embedding 源（向量检索的文本向量化层）。

约定：

- ``embed()`` 返回 **L2 归一化**向量，余弦相似度退化为点积；
- 向量只用于**召回引用**（事件 ID / 键名），不生成任何内容——
  真实结果一律由 ledger/投影按键取出（PEL 精度保证不因检索放宽）；
- 默认实现 ``FastEmbedEmbedder`` 用本地 ONNX 小模型（``fastembed`` extra），
  离线可用；``HashingEmbedder`` 是确定性测试桩，无语义。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

from project_evolution.errors import PelError

#: 默认本地模型：中英双语均衡（phase-09 真实语料 A/B 实测——中文查询不退步、
#: 英文查询显著优于 bge-small-zh；换模型只需改这里并重嵌）
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class RetrievalError(PelError):
    """向量检索不可用（缺依赖 / 模型加载失败）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.kind = "retrieval_unavailable"


class Embedder(Protocol):
    """文本向量化协议：name 用于向量索引的版本标识（换模型自动重建）。"""

    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """确定性字符 trigram 哈希向量（测试桩；无语义，仅保证稳定可复现）。"""

    def __init__(self, dim: int = 256, *, name: str = "hashing-256"):
        self._dim = dim
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        normalized = " ".join(text.lower().split())
        padded = f"  {normalized}  "
        for i in range(len(padded) - 2):
            trigram = padded[i : i + 3]
            bucket = int.from_bytes(
                hashlib.md5(trigram.encode("utf-8")).digest()[:4], "little"
            ) % self._dim
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FastEmbedEmbedder:
    """本地 ONNX 模型向量化（``pip install project-evolution[retrieval]``）。

    模型懒加载（首次 embed 才下载/加载）；向量 L2 归一化后点积即余弦。
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self._model_name = model
        self._model = None

    @property
    def name(self) -> str:
        return f"fastembed:{self._model_name}"

    def _ensure_model(self):
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RetrievalError(
                "fastembed is not installed; "
                "install with: pip install project-evolution[retrieval]"
            ) from exc
        try:
            self._model = TextEmbedding(model_name=self._model_name)
        except Exception as exc:  # noqa: BLE001 - 模型下载/加载失败统一归类
            raise RetrievalError(f"embedding model load failed: {exc}") from exc

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_model()
        assert self._model is not None  # _ensure_model 成功返回即已加载
        vectors = []
        for item in self._model.embed(list(texts)):
            vec = [float(x) for x in item]
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


def default_embedder() -> Embedder:
    """查询侧默认向量源（测试可 monkeypatch 替换）。"""
    return FastEmbedEmbedder()
