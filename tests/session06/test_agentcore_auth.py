"""The runtime contract routes spend model tokens, so they take the service key like every
other route; the probes stay open; a managed runtime that authenticates callers itself says
so with NW_RUNTIME_AUTH=platform."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nw.agent.agentcore import install_contract_auth

pytestmark = pytest.mark.session06


def _app(monkeypatch, runtime_auth: str = "") -> FastAPI:
    monkeypatch.setenv("NW_RUNTIME_AUTH", runtime_auth)
    monkeypatch.setenv("NW_RATE_LIMIT_RPS", "0")
    app = FastAPI()
    install_contract_auth(app, key='{"cohort-a": "s3cret"}')

    @app.post("/invocations")
    def invocations(request: Request) -> dict:
        return {"key_id": getattr(request.state, "api_key_id", None)}

    @app.post("/api/reasoning_engine")
    def engine() -> dict:
        return {"ok": True}

    @app.get("/ping")
    def ping() -> dict:
        return {"status": "Healthy"}

    return app


def test_contract_routes_need_the_key_and_probes_do_not(monkeypatch):
    with TestClient(_app(monkeypatch)) as c:
        assert c.post("/invocations", json={}).status_code == 401
        assert c.post("/api/reasoning_engine", json={}).status_code == 401
        ok = c.post("/invocations", json={}, headers={"x-api-key": "s3cret"})
        assert ok.status_code == 200 and ok.json()["key_id"] == "cohort-a"
        assert c.get("/ping").status_code == 200


def test_a_platform_that_authenticates_leaves_the_routes_to_it(monkeypatch):
    with TestClient(_app(monkeypatch, "platform")) as c:
        assert c.post("/invocations", json={}).status_code == 200
