"""Acceptance: the registry validates arguments, reports errors as observations,
gates irreversible tools, and exposes schemas the model can satisfy."""

import pytest

pytestmark = pytest.mark.session05


async def test_unknown_tool_and_bad_arguments_become_observations(registry):
    obs = await registry.execute("teleport", {})
    assert not obs.ok and obs.error == "unknown_tool" and "Known tools" in obs.content
    obs = await registry.execute("lookup_customer", {"account_id": "nope"})
    assert not obs.ok and obs.error == "invalid_arguments" and "account_id" in obs.content


async def test_tool_exceptions_become_observations(registry):
    obs = await registry.execute("lookup_customer", {"account_id": "NW-99999"})
    assert not obs.ok and obs.error == "tool_error" and "no account" in obs.content


async def test_entitlement_returns_a_reason(registry):
    obs = await registry.execute("check_entitlement", {"account_id": "NW-10007", "feature": "scim"})
    assert obs.ok
    assert '"entitled": false' in obs.content and "Starter" in obs.content


async def test_escalate_is_recorded_not_executed_without_approval(registry, escalation_file):
    args = {
        "ticket_id": "T-100001",
        "tier": "security",
        "justification": "Customer reports cross-tenant data in an export.",
    }
    obs = await registry.execute("escalate", args)
    assert obs.ok and obs.pending_approval and not escalation_file.exists()
    obs = await registry.execute("escalate", args, approved=True)
    assert obs.ok and not obs.pending_approval and escalation_file.exists()


def test_specs_are_strict_json_schema(registry):
    spec = next(s for s in registry.specs() if s.name == "escalate")
    assert spec.input_schema["additionalProperties"] is False
    assert set(spec.input_schema["required"]) == {"ticket_id", "tier", "justification"}
    assert "pattern" in spec.input_schema["properties"]["tier"]
