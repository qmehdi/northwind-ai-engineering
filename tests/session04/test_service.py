import json

import pytest
from fastapi.testclient import TestClient

from nw.llm.providers.fake import FakeProvider
from nw.policy import service
from nw.policy.chunking import chunk_corpus
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex

pytestmark = pytest.mark.session04


@pytest.fixture
def client(corpus_dir, make_client, monkeypatch):
    index = PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())
    good = index.retrieve("Enterprise uptime", k=1)[0].chunk.id
    fake = FakeProvider(
        [
            json.dumps(
                {
                    "answer": "99.95 percent.",
                    "citations": [good],
                    "confidence": 0.9,
                    "answerable": True,
                }
            )
        ]
    )
    monkeypatch.setattr(service, "state", service.State())
    service.state.index, service.state.client, service.state.ready = index, make_client(fake), True
    # Bypass the lifespan loader: state is injected.
    app = service.app
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def test_ask_answers_with_citations(client):
    r = client.post("/ask", json={"question": "What uptime does Enterprise get?"})
    assert r.status_code == 200
    body = r.json()
    assert not body["refused"] and body["citations"] and body["text"].startswith("99.95")


def test_ready_and_metrics(client):
    assert client.get("/readyz").json()["chunks"] > 0
    client.post("/ask", json={"question": "What uptime does Enterprise get?"})
    text = client.get("/metrics").text
    assert "nw_policy_requests_total" in text and "nw_policy_tokens_total" in text


def test_ask_is_503_with_a_reason_when_the_model_is_down(client, make_client):
    """Retries, fallback and deadline spent: the caller gets 503 and a reason, not a bare
    500, and a missing model is a 503 too."""
    from nw.llm.errors import RetryableError, TerminalError

    if service.RATE_LIMIT:  # earlier tests in the process spent this client's bucket
        service.RATE_LIMIT.reset()
    service.state.client = make_client(FakeProvider([RetryableError("down", status=503)] * 10))
    r = client.post("/ask", json={"question": "What uptime does Enterprise get?"})
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]
    service.state.client = make_client(
        FakeProvider([TerminalError("gone", status=404, code="model_not_found")] * 3)
    )
    r = client.post("/ask", json={"question": "What uptime does Enterprise get?"})
    assert r.status_code == 503 and "not available" in r.json()["detail"]
