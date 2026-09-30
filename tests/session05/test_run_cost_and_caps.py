"""Per-run cost is the run's own under concurrency; a request can lower the operator's token
cap but never raise it; `/route` honours the caps it accepts; the orchestrator forwards the
run's account to specialists and closes the HTTP client it opened; blocking screen calls run
off the event loop."""

import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from nw.agent import orchestrator, service
from nw.agent.loop import run_agent, scripted_completion
from nw.agent.router import RoutingPolicy, capped
from nw.agent.screen import Screener, Verdict
from nw.agent.trace import Termination
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05


def _script(messages, kwargs):
    """Tool calls until the run has made its quota of model calls, then an answer. The quota
    is in the task: `calls=N`."""
    task = messages[0].content
    quota = int(task.split("calls=")[1].split()[0])
    turns = sum(1 for m in messages if m.role == "assistant")
    if turns + 1 >= quota:
        return scripted_completion("Done.")
    return scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})])


async def test_concurrent_runs_are_billed_their_own_calls(registry, make_client):
    client = make_client(FakeProvider(_script, delay_s=0.01))
    a, b = await asyncio.gather(
        run_agent("Ticket T-200001 calls=2 x", registry, client, max_steps=10),
        run_agent("Ticket T-200002 calls=5 x", registry, client, max_steps=10),
    )
    assert a.cost_usd > 0 and b.cost_usd == pytest.approx(2.5 * a.cost_usd, rel=0.05)
    assert a.cost_usd + b.cost_usd == pytest.approx(client.spend_usd)
    assert sum(s.cost_usd for s in a.steps) == pytest.approx(a.cost_usd)


async def test_another_runs_spend_never_stops_this_run_on_budget(registry, make_client):
    client = make_client(FakeProvider(_script, delay_s=0.01))
    probe = await run_agent("Ticket T-200009 calls=2 x", registry, client)
    budget = probe.cost_usd * 1.5  # enough for this run's two calls, not for the neighbour's
    a, _ = await asyncio.gather(
        run_agent("Ticket T-200001 calls=2 x", registry, client, budget_usd=budget),
        run_agent("Ticket T-200002 calls=8 x", registry, client),
    )
    assert a.terminated is Termination.ANSWER


def test_a_request_can_lower_the_token_cap_never_raise_it(monkeypatch):
    monkeypatch.setattr(service, "state", service.State())
    service.state.max_total_tokens = 2000
    assert service.token_cap(50_000) == 2000
    assert service.token_cap(1500) == 1500
    assert service.token_cap(None) == 2000
    service.state.max_total_tokens = None
    assert service.token_cap(None) is None and service.token_cap(3000) == 3000


def test_route_caps_lower_the_policy_only():
    p = capped(RoutingPolicy(), max_steps=2, budget_usd=0.01)
    assert p.economy_max_steps == 2 and p.workhorse_max_steps == 2
    assert p.economy_budget_usd == 0.01 and p.workhorse_budget_usd == 0.01
    p = capped(RoutingPolicy(), max_steps=50, budget_usd=9.0)
    assert p == RoutingPolicy()


def test_route_endpoint_honours_the_requests_step_cap(make_agent_app):
    looping = FakeProvider(
        lambda m, k: scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})])
    )
    app = make_agent_app(looping)
    with TestClient(app) as c:
        r = c.post(
            "/route",
            json={
                "ticket_id": "T-200003",
                "account_id": "NW-10000",
                "subject": "Dashboard question",
                "task": "How do I share a dashboard?",
                "max_steps": 2,
            },
        )
    assert r.status_code == 200 and r.json()["terminated"] == "max_steps"
    assert len(looping.calls) == 2


def test_route_rejects_ids_that_are_not_ids(make_agent_app):
    app = make_agent_app(FakeProvider([scripted_completion("x")]))
    with TestClient(app) as c:
        bad = c.post("/route", json={"ticket_id": "../../etc", "task": "hello there"})
        assert bad.status_code == 422
        bad = c.post("/route", json={"account_id": "NW-1", "task": "hello there"})
        assert bad.status_code == 422


async def test_orchestrator_forwards_the_account_and_closes_its_client(make_client, monkeypatch):
    seen: list[dict] = []
    clients: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "run_id": "r1",
                "final": "facts",
                "terminated": "answer",
                "steps": 1,
                "cost_usd": 0.0,
                "proposed_actions": [],
            },
        )

    def fake_client(timeout: float = 30) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        clients.append(c)
        return c

    monkeypatch.setattr(orchestrator, "service_client", fake_client)
    provider = FakeProvider(
        [
            scripted_completion("", [("ask_triage", {"task": "Who is this customer?"})]),
            scripted_completion("Combined."),
        ]
    )
    t = await orchestrator.run_orchestrator(
        "Ticket T-200001", {"triage": "http://triage"}, make_client(provider), account_id="NW-10000"
    )
    assert t.final == "Combined." and seen[0]["account_id"] == "NW-10000"
    assert seen[0]["max_steps"] == orchestrator.SPECIALIST_MAX_STEPS
    assert clients and all(c.is_closed for c in clients)
    tool = orchestrator.orchestrator_registry({"triage": "http://t"}, http=clients[0]).tools
    assert tool["ask_triage"].timeout_s > orchestrator.SPECIALIST_TIMEOUT_S


class ThreadRecorder(Screener):
    name = "recorder"

    def __init__(self) -> None:
        self.threads: list[int] = []

    def screen(self, text: str) -> Verdict:
        self.threads.append(threading.get_ident())
        return Verdict(True, self.name)


async def test_the_screener_runs_off_the_event_loop(registry, make_client):
    screener = ThreadRecorder()
    client = make_client(FakeProvider([scripted_completion("Fine.")]))
    await run_agent("Ticket T-200001 calls=1 x", registry, client, screener=screener)
    assert screener.threads and threading.get_ident() not in screener.threads
