"""Acceptance: one image serves both managed runtimes. AgentCore's HTTP contract
(`/ping`, `/invocations`) and Agent Engine's (`/api/reasoning_engine`) call the same
router as the Session path's `/route`, with the Session path app mounted underneath."""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from prometheus_client import Counter, Histogram

from nw.agent import agentcore, service
from nw.agent.loop import scripted_completion
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session06


@pytest.fixture
def runtime_app(registry, make_client, monkeypatch, tmp_path):
    """The resolver with the fake registry and a fake client, no real lifespan. The
    service's metrics are process-global, so this fixture swaps in unregistered ones and
    leaves the counts other tests assert on untouched."""

    @asynccontextmanager
    async def noop(app):
        yield

    monkeypatch.setattr(
        service, "RUNS", Counter("t_runs", "x", ["role", "terminated"], registry=None)
    )
    monkeypatch.setattr(service, "STEPS", Histogram("t_steps", "x", ["role"], registry=None))
    monkeypatch.setattr(service, "LATENCY", Histogram("t_latency", "x", ["role"], registry=None))
    monkeypatch.setattr(service, "COST", Counter("t_cost", "x", ["role"], registry=None))
    monkeypatch.setattr(
        service, "PROPOSALS", Counter("t_props", "x", ["role", "tool"], registry=None)
    )
    monkeypatch.setattr(service, "state", service.State())
    service.state.role = "resolver"
    service.state.registry = registry
    service.state.trace_dir = tmp_path
    provider = FakeProvider([scripted_completion("Check the export filter.")])
    service.state.client = make_client(provider)
    service.state.ready = True
    monkeypatch.setattr(service.app.router, "lifespan_context", noop)
    return agentcore.app, provider


def test_ping_reports_health_and_role(runtime_app):
    app, _ = runtime_app
    with TestClient(app) as c:
        assert c.get("/ping").json() == {"status": "Healthy", "role": "resolver"}
        assert c.get("/readyz").json()["role"] == "resolver"
        service.state.ready = False
        assert c.get("/ping").status_code == 503


def test_invocations_route_a_confident_p0_without_a_model_call(runtime_app):
    app, provider = runtime_app
    with TestClient(app) as c:
        r = c.post(
            "/invocations",
            json={
                "prompt": "The API is down for everyone.",
                "ticket_id": "T-200005",
                "account_id": "NW-10000",
                "subject": "Production down",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cost_usd"] == 0.0 and body["proposed_actions"][0]["tool"] == "escalate"
        assert body["proposed_actions"][0]["arguments"]["tier"] == "duty_manager"
        assert provider.calls == []
        assert c.post("/invocations", json={"ticket_id": "T-1"}).status_code == 422


def test_reasoning_engine_wraps_the_same_router(runtime_app):
    app, provider = runtime_app
    with TestClient(app) as c:
        r = c.post(
            "/api/reasoning_engine",
            json={
                "class_method": "route",
                "input": {
                    "ticket_id": "T-200001",
                    "account_id": "NW-10007",
                    "subject": "Slow export",
                    "task": "Export takes 40 seconds.",
                },
            },
        )
        assert r.status_code == 200, r.text
        out = r.json()["output"]
        assert out["final"] == "Check the export filter." and out["run_id"]
        assert provider.calls[0]["model"] == "fake-workhorse"
        assert c.post("/api/reasoning_engine", json={"class_method": "query"}).status_code == 400


def test_stream_reasoning_engine_emits_one_json_line(runtime_app):
    app, _ = runtime_app
    with TestClient(app) as c:
        r = c.post(
            "/api/stream_reasoning_engine",
            json={"class_method": "route", "input": {"task": "Export takes 40 seconds."}},
        )
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in r.text.splitlines() if line]
        assert len(lines) == 1 and "output" in lines[0]


def test_session_path_routes_stay_mounted(runtime_app):
    app, _ = runtime_app
    with TestClient(app) as c:
        r = c.post("/route", json={"task": "The API is down for everyone.", "ticket_id": "T-2"})
        assert r.status_code == 200 and r.json()["cost_usd"] == 0.0
        assert "nw_agent_runs_total" in c.get("/metrics").text
