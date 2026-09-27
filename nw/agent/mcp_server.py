"""The tool registry as a Model Context Protocol server.

    uv run python -m nw.agent.mcp_server            # streamable HTTP on :8020
    NW_MCP_TRANSPORT=stdio uv run python -m nw.agent.mcp_server

Every registry tool becomes an MCP tool with the same name and the same
argument schema: the signature comes from `function_for`, generated from the
tool's Pydantic model, so a client sees `account_id` and `feature`, not one
opaque object. The approval gate travels with it: `escalate` through MCP is
still a proposal, so an MCP client, the coding agent on your laptop included,
can never page a person by itself.

Transport: DNS rebinding protection is on, and the Host header must match
`NW_MCP_ALLOWED_HOSTS` (comma separated, default `localhost:*,127.0.0.1:*`).
The server has no authentication of its own. On the Reference stack it sits
behind the track's gateway, which authenticates the caller and applies the
policy; do not expose this port directly.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from nw.agent.ports import function_for
from nw.agent.tools import Tool, ToolRegistry
from nw.logging import configure_logging, get_logger, log_fields

log = get_logger("nw.agent.mcp")

DEFAULT_ALLOWED_HOSTS = "localhost:*,127.0.0.1:*"


def allowed_hosts(value: str | None = None) -> list[str]:
    """`NW_MCP_ALLOWED_HOSTS` as a list; `host:*` accepts any port on that host."""
    raw = value if value is not None else os.environ.get("NW_MCP_ALLOWED_HOSTS", "")
    hosts = [h.strip() for h in (raw or DEFAULT_ALLOWED_HOSTS).split(",") if h.strip()]
    return hosts or DEFAULT_ALLOWED_HOSTS.split(",")


def transport_security(hosts: list[str] | None = None) -> TransportSecuritySettings:
    hosts = hosts if hosts is not None else allowed_hosts()
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")],
    )


def _mcp_function(tool: Tool, registry: ToolRegistry):
    """`function_for`'s signature, so FastMCP publishes the tool's fields as the input
    schema, with a body that goes through `registry.execute` and logs the outcome."""
    fn = function_for(tool, registry)

    async def call(**kwargs: Any) -> str:
        obs = await registry.execute(tool.name, kwargs, approved=False)
        log.info(
            "mcp call",
            extra=log_fields(tool=tool.name, ok=obs.ok, pending=obs.pending_approval),
        )
        return obs.content if obs.ok else json.dumps({"error": obs.error, "detail": obs.content})

    functools.update_wrapper(call, fn)
    call.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return call


def build_server(
    registry: ToolRegistry, name: str = "northwind-tools", *, hosts: list[str] | None = None
) -> FastMCP:
    # AgentCore's MCP contract expects a stateless streamable-HTTP server (the platform
    # injects the session id), so on the aws track the server runs without session state.
    default = "1" if os.environ.get("NW_TRACK") == "aws" else "0"
    stateless = os.environ.get("NW_MCP_STATELESS", default) == "1"
    mcp = FastMCP(
        name,
        instructions="Northwind support tools. Escalation is always a proposal.",
        transport_security=transport_security(hosts),
        stateless_http=stateless,
        json_response=stateless,
    )
    for tool in registry.tools.values():
        mcp.tool(name=tool.name, description=tool.description)(_mcp_function(tool, registry))
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
