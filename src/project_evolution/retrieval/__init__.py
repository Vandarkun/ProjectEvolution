"""向量检索（retrieval extra）：事件语义召回，只返回引用不生成内容。"""

from project_evolution.retrieval.embedder import (
    DEFAULT_MODEL,
    Embedder,
    FastEmbedEmbedder,
    HashingEmbedder,
    RetrievalError,
    default_embedder,
)
from project_evolution.retrieval.index import VectorHit, ensure_vectors, search_vectors

__all__ = [
    "DEFAULT_MODEL",
    "Embedder",
    "FastEmbedEmbedder",
    "HashingEmbedder",
    "RetrievalError",
    "VectorHit",
    "default_embedder",
    "ensure_vectors",
    "search_vectors",
]
