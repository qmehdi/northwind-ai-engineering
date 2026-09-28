"""Tool hardening: a timeout per tool, one jittered retry on transient HTTP failures and
never on a 4xx, a cap on observation size, idempotent escalation, a token budget on the
run, and the model id on every trajectory."""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from nw.agent.loop import run_agent, scripted_completion
from nw.agent.northwind import build_registry, transient_http
from nw.agent.tools import ToolRegistry, clip
from nw.agent.trace import Termination, Trajectory, replay
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

TICKET = "Ticket T-100042 from NW-10000: our SAML login is down for everyone since 8am."
ESCALATION = {
    "ticket_id": "T-100042",
    "tier": "security",
    "justification": "Confirmed breach indicators in the customer's export logs.",
}


class Nothing(BaseModel):
    pass


@pytest.fixture
def quiet(monkeypatch):
    """A registry whose retry jitter does not actually wait."""

    async def _sleep(seconds: float) -> None:
        _sleep.delays.append(seconds)

    _sleep.delays = []  # type: ignore[attr-defined]
    return _sleep


# ----- timeouts ---------------------------------------------------------------------


async def test_a_slow_tool_is_a_tool_timeout_observation(quiet):
    reg = ToolRegistry(sleep=quiet)

    @reg.tool("slow_async", "Waits too long.", timeout_s=0.05)
    async def slow_async(args: Nothing) -> str:
        await asyncio.sleep(2)
        return "never"

    @reg.tool("slow_sync", "Blocks too long.", timeout_s=0.05)
    def slow_sync(args: Nothing) -> str:
        time.sleep(0.3)
        return "never"

    for name in ("slow_async", "slow_sync"):
        obs = await reg.execute(name, {})
        assert not obs.ok and obs.error == "tool_timeout" and "0.05s" in obs.content
        assert obs.latency_ms < 250


def test_default_and_backend_timeouts(registry, tmp_path):
    assert registry.tools["lookup_customer"].timeout_s == 30.0
    reg = build_registry(
        "http", accounts_path=tmp_path / "none.json", http_client=httpx.AsyncClient()
    )
    assert reg.tools["classify_urgency"].timeout_s == 30.0
    assert reg.tools["classify_urgency"].attempts == 2


# ----- retries ----------------------------------------------------------------------


async def test_retry_runs_once_with_jitter_and_only_for_the_predicate(quiet):
    reg = ToolRegistry(sleep=quiet)
    calls = {"flaky": 0, "broken": 0}

    @reg.tool("flaky", "Fails once.", attempts=2, retry_if=lambda e: isinstance(e, ConnectionError))
    def flaky(args: Nothing) -> str:
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise ConnectionError("reset by peer")
        return "recovered"

    @reg.tool(
        "broken", "Fails for good.", attempts=2, retry_if=lambda e: isinstance(e, ConnectionError)
    )
    def broken(args: Nothing) -> str:
        calls["broken"] += 1
        raise ValueError("bad input")

    obs = await reg.execute("flaky", {})
    assert obs.ok and obs.content == "recovered" and obs.attempts == 2
    assert len(quiet.delays) == 1 and 0.05 <= quiet.delays[0] <= 0.5
    obs = await reg.execute("broken", {})
    assert not obs.ok and obs.error == "tool_error" and obs.attempts == 1 and calls["broken"] == 1


def test_transient_http_is_5xx_and_transport_errors_never_4xx():
    req = httpx.Request("POST", "http://triage/triage")

    def status(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))

    assert transient_http(status(503)) and transient_http(status(500))
    assert not transient_http(status(404)) and not transient_http(status(422))
    assert transient_http(httpx.ConnectError("refused", request=req))
    assert transient_http(httpx.ReadTimeout("slow", request=req))
    assert not transient_http(ValueError("not http at all"))


async def test_http_backend_retries_a_503_once_and_returns_a_4xx_unchanged(tmp_path, quiet):
    hits = {"triage": 0, "semantic": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/triage":
            hits["triage"] += 1
            if hits["triage"] == 1:
                return httpx.Response(503, text="warming up")
            return httpx.Response(200, json={"priority": "P0", "rule": "p0>=0.30"})
        hits["semantic"] += 1
        return httpx.Response(422, json={"detail": "body too short"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reg = build_registry("http", accounts_path=tmp_path / "none.json", http_client=client)
    reg.sleep = quiet

    obs = await reg.execute("classify_urgency", {"subject": "s", "body": "login is down"})
    assert obs.ok and '"priority": "P0"' in obs.content and obs.attempts == 2
    assert hits["triage"] == 2 and len(quiet.delays) == 1

    obs = await reg.execute("classify_semantic", {"subject": "s", "body": "x"})
    assert not obs.ok and obs.error == "tool_error" and "422" in obs.content
    assert obs.attempts == 1 and hits["semantic"] == 1, "a 4xx is never retried"


# ----- observation cap --------------------------------------------------------------


async def test_a_giant_observation_is_clipped_with_a_marker():
    reg = ToolRegistry(max_observation_chars=100)

    @reg.tool("dump", "Returns far too much.")
    def dump(args: Nothing) -> str:
        return "x" * 5000

    obs = await reg.execute("dump", {})
    assert obs.ok and obs.truncated and len(obs.content) < 200
    assert obs.content.startswith("x" * 100) and "[truncated: 5000 chars" in obs.content
    text, cut = clip("short", 100)
    assert text == "short" and cut is False
    assert ToolRegistry().max_observation_chars == 8000


async def test_the_loop_feeds_the_clipped_observation_back(registry, make_client):
    registry.max_observation_chars = 60
    provider = FakeProvider(
        [
            scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})]),
            scripted_completion("done"),
        ]
    )
    t = await run_agent("Ticket from NW-10000", registry, make_client(provider))
    fed_back = provider.calls[1]["messages"][-1].tool_results[0].content
    assert "[truncated:" in fed_back and len(fed_back) < 200
    assert "[truncated:" in (t.steps[0].observation or "")


# ----- idempotent escalation --------------------------------------------------------


async def test_escalating_the_same_ticket_twice_returns_the_earlier_proposal(
    registry, escalation_file
):
    first = await registry.execute("escalate", ESCALATION, approved=True)
    assert first.ok and '"queued": true' in first.content
    second = await registry.execute(
        "escalate", {**ESCALATION, "tier": "engineering"}, approved=True
    )
    assert second.ok
    body = json.loads(second.content)
    assert body["queued"] is False and body["duplicate"] is True
    assert body["earlier"]["tier"] == "security" and "T-100042" in body["reason"]
    assert len(escalation_file.read_text().splitlines()) == 1, "paged once"
    other = await registry.execute(
        "escalate", {**ESCALATION, "ticket_id": "T-100043"}, approved=True
    )
    assert '"queued": true' in other.content
    assert len(escalation_file.read_text().splitlines()) == 2


async def test_dedupe_window_is_configurable_and_old_records_do_not_match(
    registry, escalation_file, monkeypatch
):
    monkeypatch.setenv("NW_ESCALATION_DEDUPE_S", "0")
    await registry.execute("escalate", ESCALATION, approved=True)
    await registry.execute("escalate", ESCALATION, approved=True)
    assert len(escalation_file.read_text().splitlines()) == 2, "0 turns the window off"
    monkeypatch.setenv("NW_ESCALATION_DEDUPE_S", "3600")
    escalation_file.write_text(json.dumps(ESCALATION) + "\n")  # a record from before `ts`
    obs = await registry.execute("escalate", ESCALATION, approved=True)
    assert '"queued": true' in obs.content
    stale = {"ts": time.time() - 7200, **ESCALATION}
    escalation_file.write_text(json.dumps(stale) + "\n")
    obs = await registry.execute("escalate", ESCALATION, approved=True)
    assert '"queued": true' in obs.content, "outside the window, so it pages again"


# ----- token budget on the run ------------------------------------------------------


async def test_token_budget_stops_the_run_with_budget(registry, make_client):
    provider = FakeProvider(
        lambda msgs, kw: scripted_completion(
            "again", [("find_similar_tickets", {"body": "x"})], tokens=(200, 40)
        )
    )
    t = await run_agent(TICKET, registry, make_client(provider), max_steps=50, max_total_tokens=500)
    assert t.terminated is Termination.BUDGET and "token budget" in (t.final or "")
    assert t.tokens_total == 720 and t.n_steps == 3, "stopped before the fourth call"


# ----- governance fields on the trajectory ------------------------------------------


async def test_trajectory_carries_model_id_and_agent_version_and_replay_prints_them(
    registry, make_client, tmp_path
):
    provider = FakeProvider([scripted_completion("Nothing to do.")])
    t = await run_agent("Ticket T-200001 from NW-10000", registry, make_client(provider))
    assert t.model_id == "fake-workhorse" and len(t.agent_version or "") == 12
    saved = json.loads(t.save(tmp_path).read_text())
    assert saved["model_id"] == "fake-workhorse" and saved["agent_version"] == t.agent_version
    assert saved["tokens_total"] == 240
    text = replay(Trajectory.load(tmp_path / f"{t.run_id}.json"))
    assert re.search(r"agent_version=[0-9a-f]{12}  model_id=fake-workhorse  tokens=240", text)


def test_version_endpoint_carries_config_hash_and_the_token_cap(make_agent_app):
    provider = FakeProvider(
        lambda msgs, kw: scripted_completion(
            "again", [("find_similar_tickets", {"body": "x"})], tokens=(200, 40)
        )
    )
    app = make_agent_app(provider, max_total_tokens=300)
    with TestClient(app) as c:
        info = c.get("/version").json()
        assert re.fullmatch(r"[0-9a-f]{12}", info["config_hash"])
        assert info["max_total_tokens"] == 300 and info["fallbacks"] == {}
        assert info["resilience"]["completions"] == 0
        r = c.post("/run", json={"task": TICKET, "max_steps": 20})
        assert r.status_code == 200 and r.json()["terminated"] == "budget"
        assert c.get("/version").json()["resilience"]["completions"] == 2
