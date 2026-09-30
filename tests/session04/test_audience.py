"""Who may read internal policies is decided by the caller's key id, never by the request
body; the index filters internal chunks before ranking and fails closed on missing
metadata; feedback names its submitter and is rate limited per answer; each answer
reports its own cost; retrieval and screening run off the event loop."""

import json
import threading
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from nw.llm.providers.fake import FakeProvider
from nw.policy import service
from nw.policy.chunking import chunk_corpus, parse_document
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex

pytestmark = pytest.mark.session04


@asynccontextmanager
async def _noop(app):
    yield


class ThreadedIndex(PolicyIndex):
    def retrieve(self, *a, **kw):
        self.threads.append(threading.get_ident())
        return super().retrieve(*a, **kw)


@pytest.fixture
def served(corpus_dir, make_client, monkeypatch, tmp_path):
    index = ThreadedIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())
    index.threads = []

    def cite_first(messages, kwargs):
        ids = [
            line.split("]")[0][1:] for line in messages[-1].content.splitlines() if line[:1] == "["
        ]
        return json.dumps(
            {
                "answer": "From the policy.",
                "citations": ids[:1],
                "confidence": 0.9,
                "answerable": True,
            }
        )

    monkeypatch.setenv("NW_POLICY_FEEDBACK", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("NW_POLICY_FEEDBACK_PER_ANSWER", "2")
    monkeypatch.setenv("NW_POLICY_AUDIENCES", "")
    monkeypatch.setattr(service, "state", service.State())
    service.state.index, service.state.client, service.state.ready = (
        index,
        make_client(FakeProvider(cite_first)),
        True,
    )
    service._configure_llmops()
    service.app.router.lifespan_context = _noop
    with TestClient(service.app) as c:
        yield c, index, tmp_path


QUESTION = {"question": "What is the duty manager on-call phone for a P0?"}


def test_a_customer_key_asking_for_internal_gets_403(served):
    c, _, _ = served
    r = c.post("/ask", json={**QUESTION, "audience": "internal"})
    assert r.status_code == 403
    assert "nw_policy_forbidden_total 1.0" in c.get("/metrics").text


def test_the_body_cannot_widen_but_the_key_can(served):
    c, _, _ = served
    customer = c.post("/ask", json=QUESTION).json()
    assert not any(cid.startswith("escalation-internal") for cid in customer["context_ids"])
    service.state.audiences = {"none": "internal"}  # this caller's key id is internal now
    internal = c.post("/ask", json=QUESTION).json()
    assert any(cid.startswith("escalation-internal") for cid in internal["context_ids"])
    narrowed = c.post("/ask", json={**QUESTION, "audience": "customer"}).json()
    assert not any(cid.startswith("escalation-internal") for cid in narrowed["context_ids"])


def test_audience_map_must_be_well_formed():
    assert service.parse_audiences('{"ops": "internal"}') == {"ops": "internal"}
    assert service.parse_audiences("") == {}
    with pytest.raises(ValueError):
        service.parse_audiences('{"ops": "admin"}')


def test_internal_chunks_never_take_a_candidate_slot(corpus_dir):
    index = PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())
    hits = index.retrieve("duty manager on-call phone", k=1, candidates=1, audience="customer")
    assert hits and all(h.chunk.audience == "customer" for h in hits)


def test_missing_or_unknown_audience_fails_closed(corpus_dir):
    doc = parse_document("---\ntitle: T\ndoc_id: t\neffective: 2025-01-01\n---\n# T\n\n## A\n\nx\n")
    assert doc.audience == "internal"
    doc = parse_document(
        "---\ntitle: T\ndoc_id: t\naudience: partners\neffective: 2025-01-01\n---\n# T\n"
    )
    assert doc.audience == "internal"
    chunk = chunk_corpus(corpus_dir)[0]
    chunk.audience = "unknown"
    assert not PolicyIndex._allowed(chunk, True, "customer")
    assert PolicyIndex._allowed(chunk, True, "internal")


def test_feedback_records_the_submitter_and_is_limited_per_answer(served):
    c, _, tmp = served
    a = c.post("/ask", json={"question": "How are duplicate charges refunded?"}).json()
    for _ in range(2):
        r = c.post("/feedback", json={"answer_id": a["answer_id"], "verdict": "wrong"})
        assert r.status_code == 200
    r = c.post("/feedback", json={"answer_id": a["answer_id"], "verdict": "wrong"})
    assert r.status_code == 429
    rows = [json.loads(line) for line in (tmp / "feedback.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["submitted_by"] == "none"


def test_each_answer_reports_its_own_cost_and_retrieval_runs_in_a_thread(served):
    c, index, _ = served
    r = c.post("/ask", json={"question": "What uptime does Enterprise get?"}).json()
    assert r["cost_usd"] > 0
    assert index.threads and threading.get_ident() not in index.threads
