"""Acceptance: specialists run behind /run with a subset of the tools; the orchestrator
reaches them over HTTP, records their run ids, and surfaces proposed escalations."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nw.agent import service
from nw.agent.loop import scripted_completion
from nw.agent.orchestrator import SPECIALISTS, run_orchestrator, subset
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05


def test_specialists_get_a_subset_of_tools(registry):
    tri = subset(registry, SPECIALISTS["triage"]["tools"])
    assert "escalate" not in tri.tools and "classify_urgency" in tri.tools
    res = subset(registry, SPECIALISTS["resolution"]["tools"])
    assert "escalate" in res.tools and "search_policies" not in res.tools


@pytest.fixture
def specialist_app(registry, make_client, monkeypatch, tmp_path):
    """A triage specialist app with an injected fake client, no lifespan."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop(app):
        yield

    monkeypatch.setattr(service, "state", service.State())
    service.state.role = "triage"
    service.state.registry = registry
    service.state.trace_dir = tmp_path
    service.state.client = make_client(
        FakeProvider(
            [
                scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})]),
                scripted_completion("Blue Freight, Enterprise, P0."),
            ]
        )
    )
    service.state.ready = True
    service.app.router.lifespan_context = noop
    return service.app


def test_specialist_run_endpoint(specialist_app):
    with TestClient(specialist_app) as c:
        r = c.post("/run", json={"task": "Ticket from NW-10000: SSO down."})
        assert r.status_code == 200
        body = r.json()
        assert body["final"].startswith("Blue Freight") and body["steps"] == 2 and body["run_id"]
        assert "nw_agent_runs_total" in c.get("/metrics").text


async def test_orchestrator_calls_specialists_over_http(specialist_app, make_client):
    transport = httpx.ASGITransport(app=specialist_app)
    http = httpx.AsyncClient(transport=transport, base_url="http://triage")
    orch_client = make_client(
        FakeProvider(
            [
                scripted_completion(
                    "", [("ask_triage", {"task": "Who is NW-10000 and how urgent is this?"})]
                ),
                scripted_completion(
                    "Triage says Enterprise P0; escalation should be proposed by resolution."
                ),
            ]
        )
    )
    t = await run_orchestrator(
        "Ticket from NW-10000: SSO down.", {"triage": "http://triage"}, orch_client, http=http
    )
    assert t.tools_called == ["ask_triage"]
    obs = json.loads(t.steps[0].observation or "{}")
    assert obs["specialist"] == "triage" and obs["run_id"] and "Blue Freight" in obs["answer"]
    assert t.final and t.terminated.value == "answer"
