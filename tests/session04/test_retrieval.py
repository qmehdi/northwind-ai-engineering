"""Acceptance: hybrid retrieval finds exact terms dense misses, superseded and internal
chunks are filtered by metadata, reranking reorders by relevance."""

import pytest

from nw.policy.chunking import chunk_corpus
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex, rrf

pytestmark = pytest.mark.session04


@pytest.fixture(scope="session")
def index(corpus_dir):
    return PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())


def test_rrf_rewards_agreement():
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]])
    assert fused["a"] > fused["c"] and fused["b"] > fused["d"]
    assert max(fused, key=fused.get) in {"a", "b"}


def test_current_only_hides_the_superseded_sla(index):
    hits = index.retrieve("What uptime does the Enterprise plan get?", k=5)
    assert hits and all(r.chunk.current for r in hits)
    assert any(r.chunk.doc_id == "sla-2025" for r in hits)
    both = index.retrieve("What uptime does the Enterprise plan get?", k=8, current_only=False)
    assert any(r.chunk.doc_id == "sla-2023" for r in both)


def test_internal_documents_are_hidden_from_customers(index):
    customer = index.retrieve("duty manager on-call phone", k=5, audience="customer")
    assert all(r.chunk.audience != "internal" for r in customer)
    internal = index.retrieve("duty manager on-call phone", k=5, audience="internal")
    assert any(r.chunk.doc_id == "escalation-internal" for r in internal)


def test_lexical_catches_exact_terms(index):
    hits = index.retrieve("refund duplicate charge original payment method", k=3, rerank=False)
    assert hits[0].chunk.doc_id == "refunds"
    assert hits[0].retriever == "hybrid"


def test_rerank_puts_the_matching_section_first(index):
    hits = index.retrieve("How quickly is a P0 acknowledged for Pro customers?", k=3)
    assert hits[0].retriever == "reranked"
    assert hits[0].chunk.section == "Response times"


def test_index_round_trips(index, tmp_path):
    from nw.policy.chunking import save_chunks

    save_chunks(index.chunks, tmp_path / "chunks.jsonl")
    index.save(tmp_path, tmp_path / "chunks.jsonl")
    again = PolicyIndex.load(
        tmp_path, HashEmbeddings(), tmp_path / "chunks.jsonl", reranker=OverlapReranker()
    )
    assert [r.chunk.id for r in again.retrieve("service credits claimed within", k=2)] == [
        r.chunk.id for r in index.retrieve("service credits claimed within", k=2)
    ]
