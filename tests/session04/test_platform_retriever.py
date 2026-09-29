"""Project 3 on the platform: the retriever over the platform's vector store keeps the filters
and the rerank, and the service reports the retriever kind and the corpus id."""

import pytest
from fastapi.testclient import TestClient

from nw.config import Settings, Track
from nw.platform.base import Hit, Tenant
from nw.platform.retrievers import (
    PlatformRetriever,
    chunk_from_hit,
    corpus_id,
    platform_retriever,
    retriever_kind,
)
from nw.policy import service
from nw.policy.chunking import chunk_corpus
from nw.policy.retrieval import HashEmbeddings, OverlapReranker

pytestmark = pytest.mark.session04


class FakeVectorStore:
    """A VectorStore over the fixture chunks: cosine on hashed bags of words, the metadata
    `VectorStore.upsert` would have stored."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.embeddings = HashEmbeddings()
        self.vectors = self.embeddings.encode([c.text for c in chunks])
        self.searched = []

    def search(self, tenant, collection, query, k=8, vector=None):
        self.searched.append((tenant.prefix, collection, query, k))
        q = self.embeddings.encode([query])[0]
        scores = self.vectors @ q
        order = sorted(range(len(self.chunks)), key=lambda i: -scores[i])[:k]
        return [
            Hit(
                id=self.chunks[i].id,
                text=self.chunks[i].text,
                score=float(scores[i]),
                metadata={
                    "doc_id": self.chunks[i].doc_id,
                    "section": self.chunks[i].section,
                    "effective": self.chunks[i].effective,
                    "audience": self.chunks[i].audience,
                    "current": self.chunks[i].current,
                },
            )
            for i in order
        ]

    def count(self, tenant, collection):
        return len(self.chunks)

    def upsert(self, *a, **k):
        raise NotImplementedError

    def drop(self, *a, **k):
        raise NotImplementedError


@pytest.fixture
def chunks(corpus_dir):
    return chunk_corpus(corpus_dir)


def test_retrieve_filters_superseded_and_internal_then_reranks(chunks):
    store = FakeVectorStore(chunks)
    r = PlatformRetriever(
        store, Tenant("alice"), kind="qdrant", chunks=chunks, reranker=OverlapReranker()
    )
    top = r.retrieve("Enterprise uptime commitments", k=3)
    assert top and top[0].chunk.doc_id == "sla-2025" and top[0].retriever == "reranked"
    assert all(c.chunk.current and c.chunk.audience != "internal" for c in top)
    assert store.searched[0][:2] == ("northwind-alice", "policies")
    internal = r.retrieve("duty manager on-call phone", k=3, audience="internal")
    assert any(c.chunk.doc_id == "escalation-internal" for c in internal)
    old = r.retrieve("Enterprise uptime", k=5, current_only=False)
    assert any(c.chunk.doc_id == "sla-2023" for c in old)
    assert r.size() == len(chunks) and r.manifest_hash == "none"


def test_hits_without_local_chunks_become_chunks_from_metadata(chunks):
    store = FakeVectorStore(chunks)
    r = PlatformRetriever(
        store, Tenant("alice"), kind="rag-engine", corpus_id="projects/p/ragCorpora/1"
    )
    found = r.retrieve("refund duplicate charge", k=2)
    assert found and found[0].chunk.doc_id == "refunds" and found[0].chunk.text
    assert found[0].retriever == "dense" and 0 <= found[0].confidence <= 1
    assert r.manifest_hash == "projects/p/ragCorpora/1" and r.describe()["local_chunks"] == 0
    stale = chunk_from_hit(Hit(id="sla-2023#0", text="t", score=0.5, metadata={"current": False}))
    assert not stale.current and stale.doc_id == "sla-2023"


def test_kind_and_corpus_from_the_environment():
    assert retriever_kind({}) == "inprocess"
    assert retriever_kind({"NW_RETRIEVER": "Knowledge-Base"}) == "knowledge-base"
    with pytest.raises(ValueError):
        retriever_kind({"NW_RETRIEVER": "pinecone"})
    assert corpus_id("knowledge-base", {"NW_KNOWLEDGE_BASE_ID": "KB123"}) == "KB123"
    assert corpus_id("rag-engine", {"NW_RAG_CORPUS": "projects/p/ragCorpora/1"}).endswith("/1")
    assert corpus_id("inprocess", {}) is None


def test_platform_retriever_factory_uses_the_given_store(chunks, caplog):
    settings = Settings(track=Track.AWS, tenant="alice", _env_file=None)
    store = FakeVectorStore(chunks)
    r = platform_retriever(
        settings,
        {"NW_RETRIEVER": "qdrant", "NW_QDRANT_COLLECTION": "policies"},
        chunks=chunks,
        vectors=store,
    )
    assert r.kind == "qdrant" and r.corpus_id == "policies" and r.tenant.name == "alice"
    assert any("does not match the track" in rec.getMessage() for rec in caplog.records)
    with pytest.raises(ValueError):
        platform_retriever(settings, {"NW_RETRIEVER": "inprocess"}, vectors=store)


@pytest.fixture
def platform_service(chunks, tmp_path, monkeypatch):
    store = FakeVectorStore(chunks)
    monkeypatch.setenv("NW_RETRIEVER", "knowledge-base")
    monkeypatch.setenv("NW_KNOWLEDGE_BASE_ID", "KB123")
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.setenv("NW_POLICY_INDEX", str(tmp_path))
    monkeypatch.setenv("NW_POLICY_RERANK", "0")
    monkeypatch.setenv("NW_POLICY_FEEDBACK", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setattr(service, "state", service.State())
    # Earlier tests replace the app's lifespan with a no-op for good; this one needs the real
    # startup, so it is put back for the duration of the test.
    monkeypatch.setattr(service.app.router, "lifespan_context", service.lifespan)
    monkeypatch.setattr(
        service,
        "platform_retriever",
        lambda s, env=None, **kw: platform_retriever(
            s,
            {"NW_RETRIEVER": "knowledge-base", "NW_KNOWLEDGE_BASE_ID": "KB123"},
            vectors=store,
            **kw,
        ),
    )
    with TestClient(service.app) as c:
        yield c


def test_service_reports_the_retriever_and_the_knowledge_base(platform_service):
    ready = platform_service.get("/readyz").json()
    assert ready["status"] == "ready" and ready["chunks"] > 0
    v = platform_service.get("/version").json()
    assert v["retriever"]["kind"] == "knowledge-base" and v["retriever"]["corpus_id"] == "KB123"
    assert v["retriever"]["store"] == "FakeVectorStore" and v["tenant"] == "alice"
    assert v["gateway"] == "direct" and v["manifest"]["retriever"] == "knowledge-base"
    assert (
        platform_service.post(
            "/feedback", json={"answer_id": "nope-nope-nope", "verdict": "wrong"}
        ).status_code
        == 404
    )
