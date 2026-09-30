"""The policy service's retriever over the platform's vector store (ADR 0008).

`NW_RETRIEVER` picks where dense retrieval happens:

| value            | store                                   | corpus id variable      |
| ---------------- | --------------------------------------- | ----------------------- |
| `inprocess`      | `PolicyIndex` from `artifacts/policy`   | (none; the default)     |
| `knowledge-base` | Bedrock Knowledge Base on S3 Vectors    | `NW_KNOWLEDGE_BASE_ID`  |
| `rag-engine`     | RAG Engine on the Agent Platform        | `NW_RAG_CORPUS`         |
| `qdrant`         | Qdrant on the Local track's compose     | `NW_QDRANT_COLLECTION`  |

`PlatformRetriever` wraps `platform_for(settings).vectors.search` for the tenant's `policies`
collection and returns the same `Retrieved` objects the in-process index does, so `answer`,
the refusal bar, the drift monitor and `/feedback` are unchanged. The metadata filters (current
documents only, no internal documents for customers) and the cross-encoder rerank still run
here: the managed store returns candidates, the service decides. When the index directory ships
`chunks.jsonl` the hits are mapped back to the full chunks; otherwise the hit's text and metadata
become the chunk.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from nw.config import Settings, Track
from nw.logging import get_logger, log_fields
from nw.platform.base import Hit, Tenant, VectorStore, platform_for, tenant_from_env
from nw.policy.chunking import Chunk
from nw.policy.retrieval import Reranker, Retrieved

log = get_logger("nw.platform.retrievers")

RETRIEVER_VAR = "NW_RETRIEVER"
INPROCESS = "inprocess"
KINDS = (INPROCESS, "knowledge-base", "rag-engine", "ai-search", "qdrant")
CORPUS_VARS = {
    "knowledge-base": "NW_KNOWLEDGE_BASE_ID",
    "rag-engine": "NW_RAG_CORPUS",
    "ai-search": "NW_AZURE_SEARCH_INDEX",
    "qdrant": "NW_QDRANT_COLLECTION",
}
TRACK_KIND = {
    Track.AWS: "knowledge-base",
    Track.GCP: "rag-engine",
    Track.AZURE: "ai-search",
    Track.LOCAL: "qdrant",
}
COLLECTION = "policies"


def retriever_kind(env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    kind = (e.get(RETRIEVER_VAR) or INPROCESS).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"{RETRIEVER_VAR}={kind!r}: expected one of {KINDS}")
    return kind


def corpus_id(kind: str, env: Mapping[str, str] | None = None) -> str | None:
    e = os.environ if env is None else env
    var = CORPUS_VARS.get(kind)
    return ((e.get(var) or "").strip() or None) if var else None


def chunk_from_hit(hit: Hit, known: Mapping[str, Chunk] | None = None) -> Chunk:
    """The chunk a hit refers to: the local one when the id is known, else built from the
    hit's text and metadata (what `VectorStore.upsert` stored). A hit with no `audience` fails
    closed: it reads as internal, so a store that lost the metadata never shows an internal
    document to a customer."""
    if known and hit.id in known:
        return known[hit.id]
    m = dict(hit.metadata or {})
    superseded = m.get("superseded_by")
    if superseded is None and m.get("current") is False:
        superseded = "unknown"
    return Chunk(
        id=hit.id,
        doc_id=str(m.get("doc_id") or hit.id.split("#", 1)[0]),
        title=str(m.get("title") or m.get("doc_id") or ""),
        section=str(m.get("section") or ""),
        text=hit.text,
        effective=str(m.get("effective") or ""),
        audience=str(m.get("audience") or "internal"),
        superseded_by=str(superseded) if superseded else None,
        order=int(m.get("order") or 0),
        tokens=int(m.get("tokens") or 0),
    )


class PlatformRetriever:
    """`retrieve()` over a platform `VectorStore`, with the in-process index's filters and
    rerank. Same attributes the policy service reads: `chunks`, `manifest`, `manifest_hash`."""

    def __init__(
        self,
        vectors: VectorStore,
        tenant: Tenant,
        *,
        kind: str,
        collection: str = COLLECTION,
        corpus_id: str | None = None,
        chunks: Sequence[Chunk] | None = None,
        reranker: Reranker | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self.vectors = vectors
        self.tenant = tenant
        self.kind = kind
        self.collection = collection
        self.corpus_id = corpus_id
        self.chunks: list[Chunk] = list(chunks or [])
        self.by_id = {c.id: c for c in self.chunks}
        self.reranker = reranker
        self.manifest: dict[str, Any] = dict(manifest or {})
        self.manifest.setdefault("retriever", kind)
        self.manifest.setdefault("corpus_id", corpus_id)

    @property
    def manifest_hash(self) -> str:
        return str(self.manifest.get("corpus_sha256_12") or self.corpus_id or "none")

    def size(self) -> int:
        """Chunks the store holds for the tenant, or the local count when the store cannot say."""
        try:
            return int(self.vectors.count(self.tenant, self.collection))
        except Exception as exc:  # noqa: BLE001  a count is informational
            log.warning("vector count failed", extra=log_fields(error=str(exc)))
            return len(self.chunks)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "corpus_id": self.corpus_id,
            "collection": self.tenant.resource(self.collection),
            "store": type(self.vectors).__name__,
            "local_chunks": len(self.chunks),
        }

    def dense(self, query: str, k: int) -> list[Retrieved]:
        hits = self.vectors.search(self.tenant, self.collection, query, k=k)
        return [Retrieved(chunk_from_hit(h, self.by_id), float(h.score), "dense") for h in hits]

    def retrieve(
        self,
        query: str,
        *,
        k: int = 8,
        candidates: int = 30,
        hybrid: bool = True,
        rerank: bool = True,
        current_only: bool = True,
        audience: str = "customer",
    ) -> list[Retrieved]:
        """Candidates from the store, filtered by policy metadata, reranked, top k. `hybrid` is
        accepted for signature parity: the managed stores do their own lexical mixing."""
        pool = [
            r for r in self.dense(query, candidates) if _allowed(r.chunk, current_only, audience)
        ]
        if rerank and self.reranker is not None and pool:
            scores = self.reranker.score(query, [r.chunk.text for r in pool[: max(k * 3, 10)]])
            pool = sorted(
                (Retrieved(r.chunk, s, "reranked") for r, s in zip(pool, scores, strict=False)),
                key=lambda r: -r.score,
            )
        return pool[:k]


def _allowed(chunk: Chunk, current_only: bool, audience: str) -> bool:
    if current_only and not chunk.current:
        return False
    return not (audience == "customer" and chunk.audience == "internal")


def platform_retriever(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    chunks: Sequence[Chunk] | None = None,
    reranker: Reranker | None = None,
    manifest: Mapping[str, Any] | None = None,
    vectors: VectorStore | None = None,
) -> PlatformRetriever:
    """The retriever `NW_RETRIEVER` names, over the track's vector store (or `vectors` when
    given, which is how tests hand in a fake)."""
    e = os.environ if env is None else env
    kind = retriever_kind(e)
    if kind == INPROCESS:
        raise ValueError("NW_RETRIEVER=inprocess: use PolicyIndex, not the platform retriever")
    expected = TRACK_KIND[settings.track]
    if kind != expected:
        log.warning(
            "retriever kind does not match the track",
            extra=log_fields(retriever=kind, track=settings.track.value, expected=expected),
        )
    store = vectors if vectors is not None else platform_for(settings).vectors
    return PlatformRetriever(
        store,
        tenant_from_env(settings),
        kind=kind,
        corpus_id=corpus_id(kind, e),
        chunks=chunks,
        reranker=reranker,
        manifest=manifest,
    )
