"""The AgentCore session rule: a runtime session id is 33 to 256 characters, the header is
echoed on the response, and a missing one gets a contract-length id."""

import logging
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from prometheus_client import Counter, Histogram

from nw.agent import agentcore, service
from nw.agent.agentcore import SESSION_HEADER, SESSION_MIN_LENGTH, runtime_session_id
from nw.agent.loop import scripted_completion
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session06


@pytest.fixture
def runtime_app(registry, make_client, monkeypatch, tmp_path):
    @asynccontextmanager
    async def noop(app):
        yield

    monkeypatch.setattr(
        service, "RUNS", Counter("s_runs", "x", ["role", "terminated"], registry=None)
    )
    monkeypatch.setattr(service, "STEPS", Histogram("s_steps", "x", ["role"], registry=None))
    monkeypatch.setattr(service, "LATENCY", Histogram("s_latency", "x", ["role"], registry=None))
    monkeypatch.setattr(service, "COST", Counter("s_cost", "x", ["role"], registry=None))
    monkeypatch.setattr(
        service, "PROPOSALS", Counter("s_props", "x", ["role", "tool"], registry=None)
    )
    monkeypatch.setattr(service, "state", service.State())
    service.state.role = "resolver"
    service.state.registry = registry
    service.state.trace_dir = tmp_path
    service.state.client = make_client(
        FakeProvider([scripted_completion("Check the export filter.")])
    )
    service.state.ready = True
    monkeypatch.setattr(service.app.router, "lifespan_context", noop)
    monkeypatch.setenv("NW_TENANT", "alice")
    return agentcore.app


def test_short_seeds_are_padded_to_the_contract_length():
    padded = runtime_session_id("T-200005")
    assert padded.startswith("T-200005-") and len(padded) >= SESSION_MIN_LENGTH
    long = "a" * 40
    assert runtime_session_id(long) == long
    assert len(runtime_session_id("b" * 300)) == 256
    assert len(runtime_session_id()) >= SESSION_MIN_LENGTH


def test_invocation_echoes_the_session_header_and_logs_the_tenant(runtime_app, caplog):
    session = runtime_session_id("T-200005")
    with caplog.at_level(logging.INFO, logger="nw.agent.agentcore"), TestClient(runtime_app) as c:
        r = c.post(
            "/invocations",
            json={
                "prompt": "The API is down for everyone.",
                "ticket_id": "T-200005",
                "account_id": "NW-10000",
                "subject": "Production down",
            },
            headers={SESSION_HEADER: session},
        )
        assert r.status_code == 200, r.text
        assert r.headers[SESSION_HEADER] == session
        bare = c.post("/invocations", json={"prompt": "Export takes 40 seconds."})
        assert bare.status_code == 200 and len(bare.headers[SESSION_HEADER]) >= SESSION_MIN_LENGTH
    lines = [rec for rec in caplog.records if rec.getMessage() == "invocation"]
    assert lines and lines[0].nw["session_id"] == session and lines[0].nw["runtime"] == "agentcore"
