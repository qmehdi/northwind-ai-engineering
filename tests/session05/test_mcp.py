"""Acceptance: the registry is served over MCP with the same names, and escalation
through MCP is still a proposal."""

import pytest

from nw.agent.mcp_server import build_server

pytestmark = pytest.mark.session05


async def test_mcp_lists_registry_tools_and_gates_escalation(registry, escalation_file):
    server = build_server(registry)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"lookup_customer", "check_entitlement", "escalate", "search_policies"} <= names
    result = await server.call_tool("lookup_customer", {"arguments": {"account_id": "NW-10000"}})
    text = result[0].text if isinstance(result, list) else str(result)
    assert "Blue Freight" in text
    result = await server.call_tool(
        "escalate",
        {
            "arguments": {
                "ticket_id": "T-100001",
                "tier": "security",
                "justification": "Suspected cross-tenant data exposure in export.",
            }
        },
    )
    text = result[0].text if isinstance(result, list) else str(result)
    assert "proposed" in text and not escalation_file.exists()
