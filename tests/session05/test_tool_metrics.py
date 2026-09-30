"""AgentOps: per-tool operational metrics. The registry exposes a hook and knows nothing
about Prometheus; the service registers the counters; a specialist's subset keeps them."""

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from nw.agent.loop import scripted_completion
from nw.agent.orchestrator import SPECIALISTS, subset
from nw.agent.tools import Observation
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

ESCALATE = {
    "ticket_id": "T-100001",
    "tier": "security",
    "justification": "Customer reports cross-tenant data in an export.",
}


async def test_registry_hook_sees_every_observation(registry):
    seen: list[Observation] = []
    registry.hooks.append(seen.append)
    await registry.execute("lookup_customer", {"account_id": "NW-10000"})
    await registry.execute("lookup_customer", {"account_id": "nope"})
    await registry.execute("teleport", {})
    await registry.execute("escalate", ESCALATE)
    outcomes = [(o.tool, o.ok, o.pending_approval) for o in seen]
    assert outcomes == [
        ("lookup_customer", True, False),
        ("lookup_customer", False, False),
        ("teleport", False, False),
        ("escalate", True, True),
    ]


def test_the_registry_has_no_metrics_import():
    """The hook is the seam: the service owns Prometheus, the registry owns the tools."""
    from pathlib import Path

    import nw.agent.tools as tools_module

    assert "prometheus" not in Path(tools_module.__file__).read_text()


def test_subset_registry_keeps_the_hooks(registry):
    registry.hooks.append(lambda obs: None)
    assert subset(registry, SPECIALISTS["triage"]["tools"]).hooks == registry.hooks


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_service_counts_tool_calls_latency_and_terminations(make_agent_app):
    app = make_agent_app(
        FakeProvider(
            [
                scripted_completion(
                    "",
                    [
                        ("lookup_customer", {"account_id": "NW-10000"}),
                        ("lookup_customer", {"account_id": "NW-99999"}),
                        ("escalate", ESCALATE),
                    ],
                ),
                scripted_completion("Proposed."),
            ]
        )
    )
    ok0 = _sample("nw_agent_tool_calls_total", tool="lookup_customer", outcome="ok")
    err0 = _sample("nw_agent_tool_calls_total", tool="lookup_customer", outcome="error")
    pend0 = _sample("nw_agent_tool_calls_total", tool="escalate", outcome="pending_approval")
    lat0 = _sample("nw_agent_tool_latency_seconds_count", tool="lookup_customer")
    ans0 = _sample("nw_agent_terminations_total", terminated="answer")
    with TestClient(app) as c:
        r = c.post(
            "/run",
            json={
                "task": "Ticket T-100001 from account NW-10000: breach?",
                "account_id": "NW-10000",
            },
        )
        assert r.status_code == 200 and r.json()["terminated"] == "answer"
        text = c.get("/metrics").text
    assert _sample("nw_agent_tool_calls_total", tool="lookup_customer", outcome="ok") == ok0 + 1
    assert _sample("nw_agent_tool_calls_total", tool="lookup_customer", outcome="error") == err0 + 1
    assert (
        _sample("nw_agent_tool_calls_total", tool="escalate", outcome="pending_approval")
        == pend0 + 1
    )
    assert _sample("nw_agent_tool_latency_seconds_count", tool="lookup_customer") == lat0 + 2
    assert _sample("nw_agent_terminations_total", terminated="answer") == ans0 + 1
    assert "nw_agent_tool_latency_seconds_bucket" in text
