"""Project 4 on the platform: /version carries the tenant, the registry entry, the memory id
and the gateway; every run leaves a line with the tenant on it."""

import logging

import pytest
from fastapi.testclient import TestClient

from nw.agent.loop import scripted_completion
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05


@pytest.fixture
def platform_app(make_agent_app, monkeypatch):
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.setenv("NW_ENVIRONMENT", "northwind")
    monkeypatch.setenv(
        "NW_AGENT_REGISTRY",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:registry/r1/record/abc",
    )
    monkeypatch.setenv("NW_MEMORY_ID", "northwind_alice_memory-XYZ")
    return make_agent_app(FakeProvider([scripted_completion("Check the export filter.")]))


def test_version_reports_the_platform_fields(platform_app):
    with TestClient(platform_app) as c:
        v = c.get("/version").json()
    assert v["tenant"] == "alice" and v["environment"] == "northwind"
    assert v["registry"].endswith("/record/abc") and v["memory_id"] == "northwind_alice_memory-XYZ"
    assert v["gateway"] == "direct" and v["gateway_key"] == "unset" and v["agent_version"]


def test_every_run_logs_a_line_with_the_tenant(platform_app, caplog):
    with caplog.at_level(logging.INFO, logger="nw.agent.service"), TestClient(platform_app) as c:
        r = c.post(
            "/route", json={"task": "The API is down for everyone.", "ticket_id": "T-000002"}
        )
        assert r.status_code == 200 and r.json()["cost_usd"] == 0.0  # no model call on this path
    lines = [rec for rec in caplog.records if rec.getMessage() == "run_finished"]
    assert (
        lines and lines[-1].nw["tenant"] == "alice" and lines[-1].nw["run_id"] == r.json()["run_id"]
    )
