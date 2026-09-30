"""Private tool services: every HTTP tool call and every MCP session carries a Google ID
token for the target service when the deployment says so; a fake token source, no network."""

import httpx
import pytest

from nw.agent import mcp_client, northwind
from nw.agent.toolauth import (
    CachedTokens,
    IdTokenAuth,
    audience_of,
    mcp_headers,
    tool_auth_from_env,
)

pytestmark = pytest.mark.session05


def test_audience_is_the_service_url():
    assert audience_of("https://svc-123.run.app/triage?x=1") == "https://svc-123.run.app"


def test_id_token_auth_sets_a_bearer_per_service():
    minted: list[str] = []

    def source(audience: str) -> str:
        minted.append(audience)
        return f"tok-for-{audience}"

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), auth=IdTokenAuth(CachedTokens(source))
    )
    client.get("https://triage.run.app/triage")
    client.get("https://triage.run.app/triage")
    client.get("https://policy.run.app/ask")
    assert seen == [
        "Bearer tok-for-https://triage.run.app",
        "Bearer tok-for-https://triage.run.app",
        "Bearer tok-for-https://policy.run.app",
    ]
    assert minted == ["https://triage.run.app", "https://policy.run.app"], "cached per audience"


def test_tool_auth_follows_the_environment():
    assert tool_auth_from_env({}) is None
    assert isinstance(
        tool_auth_from_env({"NW_TOOL_AUTH": "google-id-token"}, source=lambda a: "t"), IdTokenAuth
    )
    with pytest.raises(ValueError):
        tool_auth_from_env({"NW_TOOL_AUTH": "basic"})


async def test_the_http_tools_send_the_token(monkeypatch, tmp_path):
    monkeypatch.setenv("NW_TOOL_AUTH", "google-id-token")
    monkeypatch.setenv("NW_TRIAGE_URL", "https://triage.run.app")
    monkeypatch.setattr(
        "nw.agent.toolauth._tokens", lambda source: CachedTokens(lambda a: f"id-{a}")
    )
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"priority": "P2"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reg = northwind.build_registry("http", accounts_path=tmp_path / "none.json", http_client=client)
    obs = await reg.execute("classify_urgency", {"body": "slow export"})
    assert obs.ok and seen == ["Bearer id-https://triage.run.app"]


def test_mcp_url_and_headers(monkeypatch):
    monkeypatch.delenv("NW_API_KEY", raising=False)
    assert mcp_client.mcp_url({"NW_MCP_URL": "https://mcp.run.app"}) == "https://mcp.run.app/mcp"
    assert (
        mcp_client.mcp_url({"NW_MCP_URL": "https://mcp.run.app/mcp/"}) == "https://mcp.run.app/mcp"
    )
    headers = mcp_headers(
        "https://mcp.run.app/mcp", {"NW_MCP_AUTH": "google-id-token"}, source=lambda a: f"id-{a}"
    )
    assert headers == {"authorization": "Bearer id-https://mcp.run.app"}
    assert mcp_headers("https://mcp.run.app/mcp", {}) == {}


async def test_the_mcp_backend_proxies_model_tools_and_keeps_account_tools_local(
    monkeypatch, tmp_path
):
    calls: list[tuple[str, dict]] = []

    async def fake_call(name, arguments, **kw):
        calls.append((name, arguments))
        return '{"priority": "P1"}'

    monkeypatch.setattr(mcp_client, "call_mcp_tool", fake_call)
    reg = northwind.build_registry("mcp", accounts_path=tmp_path / "none.json")
    obs = await reg.execute("classify_urgency", {"body": "slow"})
    assert obs.ok and calls == [("classify_urgency", {"subject": "", "body": "slow"})]
    assert not reg.tools["escalate"].fn.__module__.endswith("mcp_client")
    assert {"lookup_customer", "check_entitlement", "escalate"} <= set(reg.tools)
