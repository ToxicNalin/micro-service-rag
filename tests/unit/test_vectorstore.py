"""Step 1.3: embeddings helpers, tenant filters, collection bootstrap and tenant isolation in Qdrant."""

from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from qdrant_client import QdrantClient, models

from common import embeddings, vectorstore
from common.settings import clear_settings_caches
from common.vectorstore import (
    chunk_point_id,
    count_doc_points,
    delete_doc_points,
    doc_filter,
    tenant_filter,
    tenant_search_params,
)
from tests.conftest import FAKE_DIM

CHUNKS = "documents"
CACHE = "semantic_cache"


def _cond_keys(flt: models.Filter) -> list[str]:
    return [c.key for c in flt.must]  # type: ignore[union-attr]


def _tenant_cond(flt: models.Filter) -> models.FieldCondition:
    return next(c for c in flt.must if c.key == "tenant_id")  # type: ignore[union-attr]


# ---------- tenant_filter / search params ----------

def test_tenant_filter_always_contains_tenant() -> None:
    flt = tenant_filter("tenant-a")
    assert _cond_keys(flt) == ["tenant_id"]
    assert _tenant_cond(flt).match == models.MatchValue(value="tenant-a")


def test_tenant_filter_with_doc_ids_and_extra_conditions() -> None:
    extra = models.FieldCondition(key="chunk_index", range=models.Range(gte=1, lte=3))
    flt = tenant_filter("tenant-a", doc_ids=["d1", "d2"], must=[extra])
    assert _cond_keys(flt) == ["tenant_id", "doc_id", "chunk_index"]
    assert flt.must[1].match == models.MatchAny(any=["d1", "d2"])  # type: ignore[index,union-attr]
    assert _tenant_cond(flt).match.value == "tenant-a"  # type: ignore[union-attr]


def test_tenant_filter_cannot_be_dropped_by_extra_conditions() -> None:
    extra = models.FieldCondition(key="tenant_id", match=models.MatchValue(value="tenant-b"))
    flt = tenant_filter("tenant-a", must=[extra])
    # both conditions are ANDed, so a smuggled tenant can only narrow results to nothing
    assert flt.must[0].match.value == "tenant-a"  # type: ignore[index,union-attr]
    assert flt.should is None and flt.must_not is None


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_tenant_filter_rejects_empty_tenant(bad: Any) -> None:
    with pytest.raises(ValueError):
        tenant_filter(bad)


def test_doc_filter() -> None:
    flt = doc_filter("t", "d")
    assert _cond_keys(flt) == ["tenant_id", "doc_id"]


def test_tenant_search_params_uses_tenant_corpus() -> None:
    params = tenant_search_params("tenant-a")
    assert params is not None
    assert isinstance(params.idf, models.IdfCorpusParams)
    assert _tenant_cond(params.idf.corpus).match.value == "tenant-a"  # type: ignore[union-attr]


def test_tenant_search_params_honours_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PER_TENANT_IDF", "false")
    clear_settings_caches()
    assert tenant_search_params("tenant-a") is None


def test_chunk_point_id_is_deterministic() -> None:
    assert chunk_point_id("doc-1", 0) == chunk_point_id("doc-1", 0)
    assert chunk_point_id("doc-1", 0) != chunk_point_id("doc-1", 1)
    assert chunk_point_id("doc-1", 0) != chunk_point_id("doc-2", 0)
    assert chunk_point_id("doc-1", 3) == str(uuid5(NAMESPACE_URL, "doc-1:3"))


# ---------- collections ----------

def test_collections_have_expected_vectors(qdrant_mem: QdrantClient) -> None:
    chunks = qdrant_mem.get_collection(CHUNKS).config.params
    assert chunks.vectors["dense"].size == FAKE_DIM  # type: ignore[index]
    assert chunks.vectors["dense"].distance == models.Distance.COSINE  # type: ignore[index]
    assert chunks.sparse_vectors["sparse"].modifier == models.Modifier.IDF  # type: ignore[index]
    cache = qdrant_mem.get_collection(CACHE).config.params
    assert cache.vectors["q"].size == FAKE_DIM  # type: ignore[index]


def test_ensure_collections_are_idempotent(qdrant_mem: QdrantClient, fake_embeddings: Any) -> None:
    _upsert(qdrant_mem, "tenant-a", "doc-1", ["keep me"])
    vectorstore.ensure_chunks_collection(FAKE_DIM)
    vectorstore.ensure_cache_collection(FAKE_DIM)
    assert count_doc_points("tenant-a", "doc-1") == 1


def test_ensure_ignores_already_exists_race(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = QdrantClient(":memory:")
    monkeypatch.setattr(vectorstore, "client", lambda: mem)
    vectorstore.ensure_chunks_collection(FAKE_DIM)
    # simulate another service creating it between our exists-check and create
    monkeypatch.setattr(mem, "collection_exists", lambda name: False)

    def raise_exists(**_: Any) -> None:
        raise RuntimeError("Collection `documents` already exists!")

    monkeypatch.setattr(mem, "create_collection", raise_exists)
    vectorstore.ensure_chunks_collection(FAKE_DIM)  # must not raise


def test_wait_for_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    class Flaky:
        def __init__(self, fail: int) -> None:
            self.fail = fail

        def get_collections(self) -> None:
            if self.fail:
                self.fail -= 1
                raise ConnectionError("down")

    flaky = Flaky(2)
    monkeypatch.setattr(vectorstore, "client", lambda: flaky)
    vectorstore.wait_for_qdrant(retries=3, delay=0)
    monkeypatch.setattr(vectorstore, "client", lambda: Flaky(99))
    with pytest.raises(RuntimeError):
        vectorstore.wait_for_qdrant(retries=2, delay=0)


# ---------- embeddings (fakes, no downloads) ----------

def test_embed_passages_returns_lists_and_sparse_vectors(fake_embeddings: Any) -> None:
    dense, sparse = embeddings.embed_passages(["warranty lasts two years", "battery lasts 30 hours"])
    assert len(dense) == len(sparse) == 2
    assert len(dense[0]) == FAKE_DIM and isinstance(dense[0][0], float)
    assert isinstance(sparse[0], models.SparseVector)
    assert all(isinstance(i, int) for i in sparse[0].indices)
    assert embeddings.embed_passages([]) == ([], [])


def test_embed_query_and_dense_dim(fake_embeddings: Any) -> None:
    dense, sparse = embeddings.embed_query("battery life")
    assert len(dense) == embeddings.dense_dim() == FAKE_DIM
    assert sparse.values == [1.0, 1.0]
    assert embeddings.embed_dense_query("battery life") == dense
    embeddings.warmup()  # with fakes this is a no-op


def test_real_models_are_blocked_without_fakes() -> None:
    with pytest.raises(RuntimeError, match="fake_embeddings"):
        embeddings.embed_query("would download a model")


def test_batched() -> None:
    assert list(embeddings.batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(embeddings.batched([], 3)) == []
    with pytest.raises(ValueError):
        list(embeddings.batched([1], 0))


# ---------- tenant isolation in Qdrant ----------

def _upsert(mem: QdrantClient, tenant: str, doc_id: str, texts: list[str]) -> None:
    dense, sparse = embeddings.embed_passages(texts)
    mem.upsert(
        CHUNKS,
        points=[
            models.PointStruct(
                id=chunk_point_id(doc_id, i),
                vector={"dense": dense[i], "sparse": sparse[i]},
                payload={"tenant_id": tenant, "doc_id": doc_id, "chunk_index": i, "text": t},
            )
            for i, t in enumerate(texts)
        ],
        wait=True,
    )


def _hybrid(mem: QdrantClient, tenant: str, query: str) -> list[models.ScoredPoint]:
    dense, sparse = embeddings.embed_query(query)
    return mem.query_points(
        CHUNKS,
        prefetch=[
            models.Prefetch(query=dense, using="dense", filter=tenant_filter(tenant), limit=20),
            models.Prefetch(
                query=sparse, using="sparse", filter=tenant_filter(tenant),
                params=tenant_search_params(tenant), limit=20,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=20,
        with_payload=True,
    ).points


def _sparse_scores(mem: QdrantClient, tenant: str, query: str) -> dict[str, float]:
    _, sparse = embeddings.embed_query(query)
    pts = mem.query_points(
        CHUNKS, query=sparse, using="sparse", query_filter=tenant_filter(tenant),
        search_params=tenant_search_params(tenant), limit=50,
    ).points
    return {str(p.id): p.score for p in pts}


def test_hybrid_query_returns_only_own_tenant(qdrant_mem: QdrantClient, fake_embeddings: Any) -> None:
    shared = "The X200 warranty lasts two years and covers manufacturing defects."
    _upsert(qdrant_mem, "tenant-a", "doc-a", [shared, "Battery lasts 30 hours with ANC on."])
    _upsert(qdrant_mem, "tenant-b", "doc-b", [shared, "Tenant B secret pricing sheet."])

    hits = _hybrid(qdrant_mem, "tenant-a", shared)
    assert hits, "tenant A should find its own chunks"
    assert {h.payload["tenant_id"] for h in hits} == {"tenant-a"}  # type: ignore[index]
    assert hits[0].payload["doc_id"] == "doc-a"  # type: ignore[index]

    b_hits = _hybrid(qdrant_mem, "tenant-b", "secret pricing")
    assert {h.payload["tenant_id"] for h in b_hits} == {"tenant-b"}  # type: ignore[index]
    assert _hybrid(qdrant_mem, "tenant-c", shared) == []


def test_per_tenant_idf_is_unaffected_by_other_tenants(
    qdrant_mem: QdrantClient, fake_embeddings: Any
) -> None:
    _upsert(qdrant_mem, "tenant-b", "doc-b", ["valve pressure is 42 kPa", "pump manual overview"])
    query = "valve pressure"
    before = _sparse_scores(qdrant_mem, "tenant-b", query)
    assert before

    _upsert(qdrant_mem, "tenant-a", "doc-a", [f"valve pressure valve pressure note {i}" for i in range(20)])
    assert _sparse_scores(qdrant_mem, "tenant-b", query) == before


def test_shared_idf_would_leak_without_per_tenant_setting(
    qdrant_mem: QdrantClient, fake_embeddings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: with PER_TENANT_IDF=false, other tenants DO change B's scores."""
    monkeypatch.setenv("PER_TENANT_IDF", "false")
    clear_settings_caches()
    _upsert(qdrant_mem, "tenant-b", "doc-b", ["valve pressure is 42 kPa", "pump manual overview"])
    before = _sparse_scores(qdrant_mem, "tenant-b", "valve pressure")
    _upsert(qdrant_mem, "tenant-a", "doc-a", [f"valve pressure valve pressure note {i}" for i in range(20)])
    assert _sparse_scores(qdrant_mem, "tenant-b", "valve pressure") != before


def test_delete_and_count_are_tenant_scoped(qdrant_mem: QdrantClient, fake_embeddings: Any) -> None:
    # the same doc_id under two tenants (IDs differ in practice; this proves the filter, not luck)
    _upsert(qdrant_mem, "tenant-a", "doc-a", ["a one", "a two", "a three"])
    qdrant_mem.upsert(
        CHUNKS,
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-00000000000b",
                vector={"dense": embeddings.embed_dense_query("b"), "sparse": embeddings.embed_query("b")[1]},
                payload={"tenant_id": "tenant-b", "doc_id": "doc-a", "chunk_index": 0, "text": "b"},
            )
        ],
        wait=True,
    )
    assert count_doc_points("tenant-a", "doc-a") == 3
    assert count_doc_points("tenant-b", "doc-a") == 1

    delete_doc_points("tenant-b", "doc-a")
    assert count_doc_points("tenant-b", "doc-a") == 0
    assert count_doc_points("tenant-a", "doc-a") == 3

    delete_doc_points("tenant-a", "doc-a")
    assert count_doc_points("tenant-a", "doc-a") == 0


def test_reupsert_with_same_ids_does_not_duplicate(qdrant_mem: QdrantClient, fake_embeddings: Any) -> None:
    _upsert(qdrant_mem, "tenant-a", "doc-a", ["one", "two"])
    _upsert(qdrant_mem, "tenant-a", "doc-a", ["one again", "two again"])
    assert count_doc_points("tenant-a", "doc-a") == 2
