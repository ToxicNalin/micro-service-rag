"""Dense and sparse (BM25) embeddings for ingestion and retrieval; models load lazily, once per process."""

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client.models import SparseVector

from common.settings import get_vector_settings


@lru_cache
def _dense() -> TextEmbedding:
    s = get_vector_settings()
    return TextEmbedding(model_name=s.dense_model, cache_dir=s.model_cache)


@lru_cache
def _sparse() -> SparseTextEmbedding:
    s = get_vector_settings()
    return SparseTextEmbedding(model_name=s.sparse_model, cache_dir=s.model_cache)


def warmup() -> None:
    """Load both models (downloading them on the first run)."""
    _dense()
    _sparse()


def dense_dim() -> int:
    """Return the dense vector size (384 for bge-small)."""
    return int(_dense().embedding_size)


def _to_sparse(emb: Any) -> SparseVector:
    return SparseVector(indices=[int(i) for i in emb.indices], values=[float(v) for v in emb.values])


def embed_passages(texts: list[str]) -> tuple[list[list[float]], list[SparseVector]]:
    """Embed document chunks: dense vectors and BM25 sparse vectors, in input order."""
    if not texts:
        return [], []
    dense = [[float(x) for x in v] for v in _dense().passage_embed(texts, batch_size=32)]
    sparse = [_to_sparse(e) for e in _sparse().passage_embed(texts, batch_size=32)]
    return dense, sparse


def embed_query(query: str) -> tuple[list[float], SparseVector]:
    """Embed a search query (BM25 queries get no term-frequency weighting)."""
    dense = next(iter(_dense().query_embed(query)))
    sparse = next(iter(_sparse().query_embed(query)))
    return [float(x) for x in dense], _to_sparse(sparse)


def embed_dense_query(text: str) -> list[float]:
    """Return only the dense query vector (used by the semantic cache)."""
    return [float(x) for x in next(iter(_dense().query_embed(text)))]


def batched[T](items: list[T], size: int) -> Iterator[list[T]]:
    """Split a list into consecutive batches of at most `size` items."""
    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]
