"""Acceptance: the router sends confident P0s straight to escalation with no model
call, P3s to the Economy model with a small budget, and everything else to the Workhorse."""

import pytest

from nw.agent.loop import scripted_completion
from nw.agent.router import RoutingPolicy, route
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session06


@pytest.fixture
def s5_registry(registry):
    return registry


async def test_confident_p0_costs_nothing(registry, make_client):
    provider = FakeProvider([scripted_completion("should not be called")])
    client = make_client(provider)
    t = await route(
        "T-200005", "NW-10000", "Production down", "The API is down for everyone.", registry, client
    )
    assert t.agent == "router" and t.cost_usd == 0.0 and provider.calls == []
    assert t.proposed_actions and t.proposed_actions[0].arguments["tier"] == "duty_manager"


async def test_other_tickets_use_the_workhorse(registry, make_client):
    provider = FakeProvider([scripted_completion("Here is what to check.")])
    client = make_client(provider)
    t = await route(
        "T-200001", "NW-10007", "Slow export", "Export takes 40 seconds.", registry, client
    )
    assert t.agent == "resolver" and provider.calls[0]["model"] == "fake-workhorse"


async def test_economy_route_uses_the_cheap_model_and_small_caps(registry, make_client):
    """The fake triage returns P2 for ordinary text; route P2 to economy for this test."""
    provider = FakeProvider([scripted_completion("Answer.")])
    client = make_client(provider)
    policy = RoutingPolicy(economy_priorities=("P2",), economy_max_steps=2, economy_budget_usd=0.01)
    t = await route(
        "T-200015",
        "NW-10007",
        "Question",
        "How many seats on Starter?",
        registry,
        client,
        policy=policy,
    )
    assert t.agent == "resolver-economy" and provider.calls[0]["model"] == "fake-economy"


class _Block:
    name = "test-screen"

    def screen(self, text):
        from nw.agent.screen import Verdict

        return Verdict(False, self.name, "prompt attack")


async def test_router_screens_before_any_model_call(registry, make_client):
    """The screener travels with the route: a blocked ticket never reaches the Workhorse."""
    provider = FakeProvider([scripted_completion("should not be called")])
    client = make_client(provider)
    t = await route(
        "T-200001",
        "NW-10007",
        "Slow export",
        "Export takes 40 seconds.",
        registry,
        client,
        screener=_Block(),
    )
    assert provider.calls == [] and t.agent == "resolver"
    assert t.steps[0].tool == "screen:test-screen" and "Blocked" in (t.final or "")


def test_route_endpoint_observes_steps_and_latency(registry, make_client, monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from nw.agent import service

    @asynccontextmanager
    async def noop(app):
        yield

    monkeypatch.setattr(service, "state", service.State())
    service.state.role = "resolver"
    service.state.registry = registry
    service.state.trace_dir = tmp_path
    service.state.client = make_client(FakeProvider([scripted_completion("Check the export.")]))
    service.state.ready = True
    service.app.router.lifespan_context = noop
    from prometheus_client import REGISTRY

    def count(name):
        return REGISTRY.get_sample_value(name, {"role": "resolver"}) or 0.0

    steps0, latency0 = count("nw_agent_steps_count"), count("nw_agent_latency_seconds_count")
    with TestClient(service.app) as c:
        r = c.post(
            "/route",
            json={
                "ticket_id": "T-200001",
                "account_id": "NW-10007",
                "subject": "Slow export",
                "task": "Export takes 40 seconds.",
            },
        )
        assert r.status_code == 200 and r.json()["terminated"] == "answer"
    assert count("nw_agent_steps_count") == steps0 + 1
    assert count("nw_agent_latency_seconds_count") == latency0 + 1
