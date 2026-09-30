"""The agent's MCP client: the Northwind tools served by `nw.agent.mcp_server`, over
streamable HTTP, as a tool backend (`NW_TOOL_BACKEND=mcp`).

    NW_MCP_URL=https://northwind-live-mcp-123.europe-west1.run.app   # `/mcp` is appended
    NW_MCP_AUTH=google-id-token                                      # private Cloud Run

The model-backed tools (policies, triage, semantic, similar tickets) go to the server; the
customer tools and `escalate` stay in process, because they are bound to the run's account
and gated by approval here, where the run's identity is known.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from nw.agent.toolauth import TokenSource, mcp_headers
from nw.agent.tools import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

REMOTE_TOOLS = ("search_policies", "classify_urgency", "classify_semantic", "find_similar_tickets")


def mcp_url(env: Mapping[str, str] | None = None) -> str:
    raw = ((env if env is not None else os.environ).get("NW_MCP_URL") or "").strip().rstrip("/")
    if not raw:
        raise ValueError("NW_MCP_URL is not set: the MCP backend needs the server's URL")
    return raw if raw.endswith("/mcp") else f"{raw}/mcp"


def _unwrap(text: str) -> str:
    """The server already wraps results as untrusted; the loop wraps them again, once."""
    text = text.strip()
    if text.startswith(UNTRUSTED_OPEN) and text.endswith(UNTRUSTED_CLOSE):
        return text[len(UNTRUSTED_OPEN) : -len(UNTRUSTED_CLOSE)].strip()
    return text


async def call_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    source: TokenSource | None = None,
) -> str:
    """One tool call in its own session. Raises with the server's text on a tool error."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = url or mcp_url()
    headers = headers if headers is not None else mcp_headers(url, source=source)
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    text = "".join(getattr(c, "text", "") or "" for c in result.content)
    if result.isError:
        raise RuntimeError(text or f"{name} failed on the MCP server")
    return _unwrap(text)
