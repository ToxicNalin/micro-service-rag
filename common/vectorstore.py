"""Qdrant client, collection bootstrap and the ONLY place that builds tenant filters (invariant I2).

Other modules must call these helpers through the module (e.g. `vectorstore.client()`), so tests
can patch `common.vectorstore.client` with an in-memory client.
"""

import logging
import time
from functools import lru_cache
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from common.settings import get_vector_settings

log = logging.getLogger(__name__)

_HNSW = models.HnswConfigDiff(m=0, payload_m=16)  # per-tenant graphs only, no global graph
_TENANT_INDEX = models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True)


@lru_cache
def client() -> QdrantClient:
    """Return the cached Qdrant client."""
    return QdrantClient(url=get_vector_settings().qdrant_url, timeout=60)


def wait_for_qdrant(retries: int = 30, delay: float = 2.0) -> None:
    """Block until Qdrant answers get_collections(); raise RuntimeError after the last retry."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client().get_collections()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            last = exc
            log.warning("qdrant not ready", extra={"attempt": attempt, "retries": retries})
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(f"Qdrant unreachable after {retries} attempts") from last


def _create_if_missing(name: str, **kwargs: Any) -> None:
    c = client()
    if c.collection_exists(name):
        return
    try:
        c.create_collection(collection_name=name, hnsw_config=_HNSW, **kwargs)
        log.info("created qdrant collection", extra={"collection": name})
    except Exception as exc:
        # ingestion and retrieval may race to create the same collection at startup
        if "already exists" not in str(exc).lower():
            raise


def ensure_chunks_collection(dim: int) -> None:
    """Create the chunks collection and its payload indexes if missing (idempotent)."""
    name = get_vector_settings().chunks_collection
    _create_if_missing(
        name,
        vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    c = client()
    c.create_payload_index(name, "tenant_id", field_schema=_TENANT_INDEX)
    c.create_payload_index(name, "doc_id", field_schema=models.PayloadSchemaType.KEYWORD)
    c.create_payload_index(name, "chunk_index", field_schema=models.PayloadSchemaType.INTEGER)


def ensure_cache_collection(dim: int) -> None:
    """Create the semantic-cache collection and its payload indexes if missing (idempotent)."""
    name = get_vector_settings().cache_collection
    _create_if_missing(
        name,
        vectors_config={"q": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
    )
    c = client()
    c.create_payload_index(name, "tenant_id", field_schema=_TENANT_INDEX)
    c.create_payload_index(name, "scope_key", field_schema=models.PayloadSchemaType.KEYWORD)
    c.create_payload_index(name, "expires_at", field_schema=models.PayloadSchemaType.FLOAT)


def tenant_filter(
    tenant_id: str,
    *,
    doc_ids: list[str] | None = None,
    must: list[Any] | None = None,
) -> models.Filter:
    """Build a filter that always restricts to one tenant, plus optional doc IDs and extra conditions."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required")
    conditions: list[Any] = [models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))]
    if doc_ids is not None:
        # an explicit empty list matches nothing, never "all documents"
        conditions.append(models.FieldCondition(key="doc_id", match=models.MatchAny(any=list(doc_ids))))
    if must:
        conditions.extend(must)
    return models.Filter(must=conditions)


def tenant_search_params(tenant_id: str) -> models.SearchParams | None:
    """Search params that compute BM25 IDF over this tenant's points only (invariant I3)."""
    if not get_vector_settings().per_tenant_idf:
        return None
    return models.SearchParams(idf=models.IdfCorpusParams(corpus=tenant_filter(tenant_id)))


def doc_filter(tenant_id: str, doc_id: str) -> models.Filter:
    """Filter for one document of one tenant."""
    return tenant_filter(tenant_id, doc_ids=[doc_id])


def delete_doc_points(tenant_id: str, doc_id: str) -> None:
    """Delete all chunks of one document of one tenant."""
    client().delete(
        collection_name=get_vector_settings().chunks_collection,
        points_selector=models.FilterSelector(filter=doc_filter(tenant_id, doc_id)),
        wait=True,
    )


def count_doc_points(tenant_id: str, doc_id: str) -> int:
    """Return the exact number of chunks stored for one document of one tenant."""
    result = client().count(
        collection_name=get_vector_settings().chunks_collection,
        count_filter=doc_filter(tenant_id, doc_id),
        exact=True,
    )
    return int(result.count)


def chunk_point_id(doc_id: str, chunk_index: int) -> str:
    """Deterministic point ID, so re-ingesting a document overwrites the same points."""
    return str(uuid5(NAMESPACE_URL, f"{doc_id}:{chunk_index}"))
