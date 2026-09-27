"""Acceptance: the registry is served over MCP with the same names and the same
argument schemas, escalation through MCP is still a proposal, and the transport
only answers to the configured hosts."""

import pytest

from nw.agent.mcp_server import allowed_hosts, build_server, transport_security

pytestmark = pytest.mark.session05


async def test_mcp_lists_registry_tools_and_gates_escalation(registry, escalation_file):
    server = build_server(registry)
    tools = {t.name: t for t in await server.list_tools()}
    assert {"lookup_customer", "check_entitlement", "escalate", "search_policies"} <= set(tools)
    result = await server.call_tool("lookup_customer", {"account_id": "NW-10000"})
    text = result[0].text if isinstance(result, list) else str(result)
    assert "Blue Freight" in text
    result = await server.call_tool(
        "escalate",
        {
            "ticket_id": "T-100001",
            "tier": "security",
            "justification": "Suspected cross-tenant data exposure in export.",
        },
    )
    text = result[0].text if isinstance(result, list) else str(result)
    assert "proposed" in text and not escalation_file.exists()


async def test_mcp_publishes_the_tools_fields_as_the_input_schema(registry):
    """One opaque `arguments` object would let a client send anything; the schema must name
    the fields, generated from the same Pydantic model the loop validates against."""
    tools = {t.name: t for t in await build_server(registry).list_tools()}
    assert set(tools["escalate"].inputSchema["properties"]) == {
        "ticket_id",
        "tier",
        "justification",
    }
    assert set(tools["check_entitlement"].inputSchema["properties"]) == {"account_id", "feature"}
    assert set(tools["escalate"].inputSchema.get("required", [])) == {
        "ticket_id",
        "tier",
        "justification",
    }


async def test_mcp_rejects_bad_arguments_as_an_observation(registry):
    result = await build_server(registry).call_tool(
        "lookup_customer", {"account_id": "blue freight"}
    )
    text = result[0].text if isinstance(result, list) else str(result)
    assert "invalid_arguments" in text


def test_transport_is_locked_to_the_configured_hosts(registry, monkeypatch):
    monkeypatch.delenv("NW_MCP_ALLOWED_HOSTS", raising=False)
    assert allowed_hosts() == ["localhost:*", "127.0.0.1:*"]
    server = build_server(registry)
    assert server.settings.transport_security is not None
    assert server.settings.transport_security.allowed_hosts == ["localhost:*", "127.0.0.1:*"]
    monkeypatch.setenv("NW_MCP_ALLOWED_HOSTS", "mcp.internal:8020, 10.0.0.5:*")
    assert allowed_hosts() == ["mcp.internal:8020", "10.0.0.5:*"]
    settings = transport_security(["localhost:*"])
    assert settings.enable_dns_rebinding_protection and settings.allowed_hosts == ["localhost:*"]
    assert "http://localhost:*" in settings.allowed_origins
