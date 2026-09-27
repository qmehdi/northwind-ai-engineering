"""Offline checks on the framework bridges: the generated function carries the
registry's fields, and both frameworks accept it as a tool with the right schema.
Running the ports end to end needs a cloud account and is a `live` test."""

import inspect

import pytest

from nw.agent.ports import function_for, is_irreversible

pytestmark = pytest.mark.session05


def test_function_signature_mirrors_the_model(registry):
    fn = function_for(registry.tools["escalate"], registry)
    params = inspect.signature(fn).parameters
    assert list(params) == ["ticket_id", "tier", "justification"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert "Args:" in (fn.__doc__ or "") and fn.__name__ == "escalate"


async def test_generated_function_goes_through_the_registry_gate(registry, escalation_file):
    fn = function_for(registry.tools["escalate"], registry)
    out = await fn(
        ticket_id="T-100001", tier="security", justification="Suspected cross-tenant data exposure."
    )
    assert "proposed action" in out and not escalation_file.exists()
    bad = await fn(ticket_id="nope", tier="security", justification="x" * 30)
    assert "invalid_arguments" in bad


def test_strands_accepts_the_generated_tool(registry):
    pytest.importorskip("strands")
    from strands import tool

    t = tool(function_for(registry.tools["check_entitlement"], registry, sync=True))
    spec = t.tool_spec
    props = spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"account_id", "feature"}
    assert is_irreversible(registry, "escalate") and not is_irreversible(
        registry, "check_entitlement"
    )


def test_adk_accepts_the_generated_tool(registry):
    pytest.importorskip("google.adk")
    from google.adk.tools import FunctionTool

    ft = FunctionTool(
        function_for(registry.tools["lookup_customer"], registry), require_confirmation=True
    )
    decl = ft._get_declaration()
    assert decl.name == "lookup_customer"
    schema = decl.parameters_json_schema or decl.parameters.model_dump()
    assert "account_id" in schema["properties"]


def test_adk_port_reports_how_the_runner_ended():
    """The runner yields events, never a termination. A run with no final text is not an
    answer: paused on the confirmation gate it counts as a proposal, otherwise an error."""
    from nw.agent.ports.adk_port import finish
    from nw.agent.trace import Termination, Trajectory

    t = Trajectory(run_id="a", agent="resolver-adk", task="t")
    finish(t, ["Escalation proposed to engineering."])
    assert t.terminated is Termination.ANSWER and t.final.startswith("Escalation")
    t = Trajectory(run_id="b", agent="resolver-adk", task="t")
    finish(t, [], paused_on="escalate")
    assert t.terminated is Termination.ANSWER and "escalate" in t.final and "proposed" in t.final
    t = Trajectory(run_id="c", agent="resolver-adk", task="t")
    finish(t, [""])
    assert t.terminated is Termination.ERROR and "without a final response" in t.final
