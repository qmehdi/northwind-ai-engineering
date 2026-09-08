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
