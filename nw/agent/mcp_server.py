"""The tool registry as a Model Context Protocol server.

    uv run python -m nw.agent.mcp_server            # streamable HTTP on :8020
    NW_MCP_TRANSPORT=stdio uv run python -m nw.agent.mcp_server

Every registry tool becomes an MCP tool with the same name and schema. The
approval gate travels with it: `escalate` through MCP is still a proposal, so
an MCP client, the coding agent on your laptop included, can never page a
person by itself. On the Reference stack this server sits behind the track's
gateway with a policy in front of it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from nw.agent.tools import ToolRegistry
from nw.logging import configure_logging, get_logger, log_fields

log = get_logger("nw.agent.mcp")


def build_server(registry: ToolRegistry, name: str = "northwind-tools") -> FastMCP:
    mcp = FastMCP(name, instructions="Northwind support tools. Escalation is always a proposal.")
    for tool in registry.tools.values():

        def make(t):
            async def call(arguments: dict[str, Any]) -> str:
                obs = await registry.execute(t.name, arguments, approved=False)
                log.info(
                    "mcp call",
                    extra=log_fields(tool=t.name, ok=obs.ok, pending=obs.pending_approval),
                )
                return (
                    obs.content
                    if obs.ok
                    else json.dumps({"error": obs.error, "detail": obs.content})
                )

            call.__name__ = t.name
            call.__doc__ = t.description
            # FastMCP builds the schema from the signature; we hand it the registry's schema
            # explicitly through a single `arguments` object so the two can never drift.
            mcp.tool(name=t.name, description=t.description)(call)

        make(tool)
    return mcp


def main() -> int:
    configure_logging(os.environ.get("NW_LOG_FORMAT", "text"))
    from nw.agent.northwind import build_registry

    registry = build_registry(os.environ.get("NW_TOOL_BACKEND", "local"))
    server = build_server(registry)
    transport = os.environ.get("NW_MCP_TRANSPORT", "streamable-http")
    if transport == "streamable-http":
        server.settings.host = os.environ.get("NW_MCP_HOST", "127.0.0.1")
        server.settings.port = int(os.environ.get("NW_MCP_PORT", "8020"))
    server.run(transport=transport)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
