"""The managed-runtime contract routes (AgentCore `/invocations`, Agent Engine
`/api/reasoning_engine`) take the same API key and rate limit as `/route`, counted once."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nw.auth import protect

pytestmark = pytest.mark.session06


def test_protect_keys_and_limits_only_the_named_paths(monkeypatch):
    monkeypatch.setenv("NW_API_KEY", "s3cret")
    monkeypatch.setenv("NW_RATE_LIMIT_RPS", "0.001")
    monkeypatch.setenv("NW_RATE_LIMIT_BURST", "2")
    app = FastAPI()
    protect(app, {"/invocations"})

    @app.post("/invocations")
    def invocations():
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"status": "Healthy"}

    key = {"x-api-key": "s3cret"}
    with TestClient(app) as c:
        assert c.post("/invocations").status_code == 401
        assert [c.post("/invocations", headers=key).status_code for _ in range(3)] == [
            200,
            200,
            429,
        ]
        assert all(c.get("/ping").status_code == 200 for _ in range(5)), "probe untouched"


@pytest.fixture
def keyed_agentcore(monkeypatch):
    monkeypatch.setenv("NW_API_KEY", "s3cret")
    from nw.agent import agentcore

    module = importlib.reload(agentcore)
    yield module
    monkeypatch.delenv("NW_API_KEY")
    importlib.reload(agentcore)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/invocations", {"prompt": "hello"}),
        ("/api/reasoning_engine", {"class_method": "route", "input": {"task": "hello"}}),
        ("/api/stream_reasoning_engine", {"class_method": "route", "input": {"task": "hello"}}),
    ],
)
def test_contract_routes_refuse_a_missing_key(keyed_agentcore, path, body):
    c = TestClient(keyed_agentcore.app)  # no lifespan: the key check answers first
    assert c.post(path, json=body).status_code == 401
    assert c.post(path, json=body, headers={"x-api-key": "wrong"}).status_code == 401
    assert c.get("/ping").status_code in (200, 503), "the runtime's probe needs no key"
