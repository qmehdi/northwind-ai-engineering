"""Acceptance: citations are validated in code, weak retrieval refuses, and the
harness separates retrieval from generation and gates regressions."""

import json

import pytest

from nw.llm.providers.fake import FakeProvider
from nw.policy.answer import REFUSAL, answer
from nw.policy.chunking import chunk_corpus
from nw.policy.evaluate import (
    EvalCase,
    aggregate,
    citation_validity,
    evaluate,
    gate,
    retrieval_metrics,
)
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex

pytestmark = pytest.mark.session04


@pytest.fixture(scope="session")
def index(corpus_dir):
    return PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())


def draft(text, citations, answerable=True, confidence=0.9):
    return json.dumps(
        {"answer": text, "citations": citations, "confidence": confidence, "answerable": answerable}
    )


async def test_valid_citations_are_kept_and_invented_ones_dropped(index, make_client):
    hits = index.retrieve("Enterprise uptime", k=3)
    good, bad = hits[0].chunk.id, "sla-2025#deadbeef0000"
    client = make_client(FakeProvider([draft("Enterprise gets 99.95 percent.", [good, bad])]))
    a = await answer(client, "Enterprise uptime?", hits)
    assert not a.refused and a.citations == [good] and a.dropped_citations == [bad]
    assert citation_validity(a) == 0.5


async def test_answer_with_no_valid_citation_is_refused(index, make_client):
    hits = index.retrieve("Enterprise uptime", k=3)
    client = make_client(FakeProvider([draft("Something from memory.", ["not-a-chunk"])]))
    a = await answer(client, "Enterprise uptime?", hits)
    assert a.refused and a.reason == "no_valid_citation" and a.text == REFUSAL


async def test_weak_retrieval_refuses_without_calling_the_model(index, make_client):
    provider = FakeProvider([draft("should not be called", [])])
    client = make_client(provider)
    hits = index.retrieve("quantum teleportation pricing", k=3)
    a = await answer(client, "quantum teleportation pricing?", hits, min_score=0.9)
    assert a.refused and a.reason == "weak_retrieval"
    assert provider.calls == []


async def test_model_may_declare_unanswerable(index, make_client):
    hits = index.retrieve("Enterprise uptime", k=3)
    client = make_client(FakeProvider([draft("", [], answerable=False, confidence=0.2)]))
    a = await answer(client, "What colour is the office?", hits)
    assert a.refused and a.reason == "model_unanswerable"


def test_retrieval_metrics():
    class R:
        def __init__(self, cid):
            self.chunk = type("C", (), {"id": cid})()

    recall, mrr = retrieval_metrics([R("a"), R("b"), R("c")], ["b", "z"])
    assert recall == 0.5 and mrr == 0.5
    assert retrieval_metrics([R("a")], []) == (1.0, 1.0)


async def test_harness_separates_retrieval_from_generation_and_gates(index, make_client):
    good = index.retrieve("Enterprise uptime", k=3)[0].chunk.id
    script = [
        draft("Enterprise gets 99.95 percent.", [good]),  # case 1 answer
        json.dumps({"faithfulness": 1.0, "reason": "supported"}),  # case 1 judge
        draft("", [], answerable=False),  # case 2: must refuse
    ]
    client = make_client(FakeProvider(script), max_concurrency=1)
    cases = [
        EvalCase(id="1", question="Enterprise uptime?", gold_chunk_ids=[good]),
        EvalCase(id="2", question="What colour is the office?", must_refuse=True),
    ]
    results, agg = await evaluate(client, index, cases, k=3, concurrency=1)
    assert (
        agg["recall_at_k"] == 1.0
        and agg["citation_validity"] == 1.0
        and agg["refusal_correct"] == 1.0
    )
    assert agg["faithfulness"] == 1.0 and agg["faithfulness_n"] == 1
    assert agg["cost_usd_total"] > 0
    assert gate(agg, agg) == []
    worse = dict(agg, recall_at_k=agg["recall_at_k"] - 0.1)
    assert any("recall_at_k" in f for f in gate(worse, agg))
    assert any("citation_validity" in f for f in gate(dict(agg, citation_validity=0.9), agg))
    assert aggregate(results)["n"] == 2
