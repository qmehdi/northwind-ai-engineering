"""The agent container as the runtime: register writes the card and tags, invoke is HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from nw.platform.base import AgentRuntime
from nw.platform.local import LocalAgentRuntime

pytestmark = pytest.mark.session06

CARD = {
    "name": "resolver",
    "kind": "resolver",
    "agent_version": "d151264eec33",
    "prompt": "system@e645e8a6b564",
    "tools_version": "e0d56e06da2b",
    "use_case": "ticket-resolution",
    "owner": "support-platform",
    "risk_class": "medium",
    "approval": {"state": "approved", "by": "cx-lead"},
}


def test_register_writes_the_card_and_mlflow_tags(mlflow_uri, tenant, tmp_path, compose):
    runtime = LocalAgentRuntime("http://agent.test", tmp_path / "registry", compose, mlflow_uri)
    assert isinstance(runtime, AgentRuntime)
    rid = runtime.register(tenant, CARD)
    assert rid == "local://agents/northwind-alice/d151264eec33"
    card = json.loads((tmp_path / "registry" / "northwind-alice.json").read_text())
    assert (
        card["runtime_id"] == rid
        and card["tenant"] == "alice"
        and card["owner"] == "support-platform"
    )
    from mlflow import MlflowClient

    rm = MlflowClient(tracking_uri=mlflow_uri, registry_uri=mlflow_uri).get_registered_model(
        "northwind-alice-agent"
    )
    assert rm.tags["agent_version"] == "d151264eec33" and rm.tags["approval_state"] == "approved"


def test_deploy_writes_env_and_recreates_the_container(tenant, tmp_path, compose):
    runtime = LocalAgentRuntime("http://agent.test", tmp_path / "registry", compose, None)
    rid = runtime.deploy(
        tenant, "localhost:5050/nw-agent:abc", {"NW_AGENT_ROLE": "resolver"}, version="v7"
    )
    assert rid == "local://resolver/v7"
    env = (tmp_path / "registry" / "northwind-alice-agent.env").read_text()
    assert (
        "NW_AGENT_VERSION=v7\n" in env
        and "NW_AGENT_ROLE=resolver\n" in env
        and "NW_TENANT=alice\n" in env
    )
    assert ("up", "-d", "--no-deps", "--force-recreate", "resolver") in compose.calls


def test_invoke_and_status_over_http(tenant, tmp_path, compose):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/invocations":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "answer": f"handled {body['task']}",
                    "session": request.headers.get("x-session-id"),
                },
            )
        if request.url.path == "/ping":
            return httpx.Response(200, json={"status": "Healthy"})
        if request.url.path == "/version":
            return httpx.Response(200, json={"agent_version": "d151264eec33"})
        return httpx.Response(404)

    runtime = LocalAgentRuntime(
        "http://agent.test",
        tmp_path / "registry",
        compose,
        None,
        api_key="k1",
        transport=httpx.MockTransport(handler),
    )
    out = runtime.invoke(tenant, {"task": "refund T-1"}, session_id="s-42")
    assert out == {"answer": "handled refund T-1", "session": "s-42"}
    assert (
        calls[0].headers["x-api-key"] == "k1" and calls[0].headers["x-tenant"] == "northwind-alice"
    )
    st = runtime.status(tenant)
    assert (
        st["healthy"] is True
        and st["version"]["agent_version"] == "d151264eec33"
        and st["card"] is False
    )
