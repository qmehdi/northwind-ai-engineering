"""AgentOps: the agent version is a hash of the prompt, the tool specs and the model ids.
It moves when any of them moves, and it is on every trajectory, in the report, on the
`agent.run` span and on the service's /version."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nw import telemetry
from nw.agent.evaluate import AgentCase, aggregate, score
from nw.agent.loop import SYSTEM_RULES, run_agent, scripted_completion
from nw.agent.version import agent_version, describe, prompt_version, tool_fingerprint
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

MODELS = {"workhorse": "fake-workhorse"}


def test_changing_a_tool_description_changes_the_version(registry):
    before = agent_version(SYSTEM_RULES, registry.specs(), MODELS)
    assert len(before) == 12 and before == agent_version(SYSTEM_RULES, registry.specs(), MODELS)
    registry.tools["escalate"].description += " Also pages the duty manager on weekends."
    after = agent_version(SYSTEM_RULES, registry.specs(), MODELS)
    assert after != before


def test_prompt_and_model_change_it_too_but_order_does_not(registry):
    base = agent_version(SYSTEM_RULES, registry.specs(), MODELS)
    assert agent_version(SYSTEM_RULES + "\nBe brief.", registry.specs(), MODELS) != base
    assert agent_version(SYSTEM_RULES, registry.specs(), {"workhorse": "fake-economy"}) != base
    reversed_specs = list(reversed(registry.specs()))
    assert tool_fingerprint(reversed_specs) == tool_fingerprint(registry.specs())
    info = describe(SYSTEM_RULES, registry.specs(), MODELS)
    assert info["agent_version"] == base and "escalate" in info["tools"]
    assert info["prompt"].startswith("system@") and prompt_version("x", "a") != prompt_version(
        "x", "b"
    )


async def test_version_is_on_the_trajectory_the_span_and_the_report(registry, make_client):
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter=exporter, env={})
    provider = FakeProvider([scripted_completion("Nothing to do.")])
    t = await run_agent("Ticket T-200001 from account NW-10000", registry, make_client(provider))
    expected = agent_version(SYSTEM_RULES, registry.specs(), MODELS)
    assert t.agent_version == expected
    telemetry.trace.get_tracer_provider().force_flush()
    run_span = next(s for s in exporter.get_finished_spans() if s.name == "agent.run")
    assert run_span.attributes["nw.agent_version"] == expected
    case = AgentCase(
        id="x",
        kind="k",
        account_id="NW-10000",
        ticket_id="T-200001",
        subject="s",
        body="b",
        expect={"must_not_escalate": True},
    )
    agg = aggregate([score(case, t)], t.agent_version)
    assert agg["agent_version"] == expected


def test_version_endpoint_matches_the_trajectory(make_agent_app, registry):
    app = make_agent_app(FakeProvider([scripted_completion("Done.")]))
    with TestClient(app) as c:
        info = c.get("/version").json()
        assert info["role"] == "resolver" and info["models"] == MODELS
        assert info["agent_version"] == agent_version(SYSTEM_RULES, registry.specs(), MODELS)
        assert info["disabled"] is False and info["max_concurrent_runs"] == 4
        r = c.post("/run", json={"task": "Ticket T-200001 from account NW-10000: hello"})
        assert r.status_code == 200
    from nw.agent import service
    from nw.agent.trace import Trajectory

    saved = Trajectory.load(next(service.state.trace_dir.glob("*.json")))
    assert saved.agent_version == info["agent_version"]
