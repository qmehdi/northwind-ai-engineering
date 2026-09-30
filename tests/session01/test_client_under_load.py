"""The service layer under load: a token budget checked when a prompt has its turn, slots given
back during a back-off, an overall deadline per call, cost attributed to the run that spent it,
refusals metered, and a meter whose memory is bounded. The fake provider yields on every call,
so these tests schedule the way a real provider does."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nw.llm.cost import CostMeter
from nw.llm.errors import (
    ContentFilteredError,
    RetryableError,
    TerminalError,
    TokenBudgetExceeded,
)
from nw.llm.providers import anthropic_base as ab
from nw.llm.providers import bedrock_converse as bc
from nw.llm.providers import openai_compat as oc
from nw.llm.providers.fake import FakeProvider
from nw.llm.retry import RetryPolicy
from nw.llm.types import Completion, StopReason, Usage

pytestmark = pytest.mark.session01


async def test_token_budget_is_checked_when_a_prompt_gets_its_turn(make_client):
    # Two slots, 150 tokens a call, a budget of 300: the first two calls use it up and the
    # four prompts behind them are never sent. Checked at task creation, all six would go.
    provider = FakeProvider(tokens_per_call=(100, 50), delay_s=0.01)
    client = make_client(provider, max_concurrency=2)

    results = await client.map(
        [f"p{i}" for i in range(6)], max_total_tokens=300, return_exceptions=True
    )

    assert len(provider.calls) == 2
    assert sum(isinstance(r, TokenBudgetExceeded) for r in results) == 4


async def test_a_backoff_gives_its_slot_to_another_caller(make_client):
    # One slot. The first call is rate limited and told to wait; while it waits, the second
    # call must get the slot and finish first.
    def script(messages, kw):
        if messages[-1].content == "slow" and kw["index"] == 0:
            return RetryableError("rate limited", status=429, retry_after_s=0.05)
        return f"done:{messages[-1].content}"

    provider = FakeProvider(script)
    client = make_client(provider, max_concurrency=1, sleep=asyncio.sleep)
    finished: list[str] = []

    async def call(text: str) -> None:
        await client.complete(text)
        finished.append(text)

    await asyncio.gather(call("slow"), call("fast"))
    assert finished == ["fast", "slow"], "the sleeping retry held the only slot"
    assert provider.high_water == 1


async def test_the_call_deadline_stops_the_retries(make_client, no_sleep):
    provider = FakeProvider([RetryableError("busy", status=503, retry_after_s=4.0)] * 10)
    client = make_client(provider, retry=RetryPolicy(max_attempts=10), deadline_s=5.0)
    with pytest.raises(RetryableError):
        await client.complete("q")
    # Slept 4 s after the first failure; a second 4 s back-off would pass the 5 s deadline.
    assert len(provider.calls) == 2 and no_sleep.delays == [4.0]


async def test_concurrent_runs_see_only_their_own_cost(make_client):
    provider = FakeProvider(tokens_per_call=(100, 50), delay_s=0.005)
    client = make_client(provider, max_concurrency=4)

    async def run(calls: int):
        with client.cost_scope() as scope:
            for i in range(calls):
                await client.complete(f"step {i}")
            return scope

    a, b = await asyncio.gather(run(2), run(3))
    assert len(a) == 2 and len(b) == 3
    assert a.tokens == 300 and b.tokens == 450
    assert a.total_usd + b.total_usd == pytest.approx(client.spend_usd)
    assert b.total_usd == pytest.approx(a.total_usd * 1.5)


async def test_a_completion_carries_its_cost_but_does_not_serialise_it(make_client):
    client = make_client(FakeProvider(tokens_per_call=(1000, 500)))
    completion = await client.complete("hello")
    assert completion.cost is not None and completion.cost_usd > 0
    assert completion.cost_usd == pytest.approx(client.spend_usd)
    assert "cost" not in completion.model_dump()


async def test_a_refusal_is_metered_and_attributed(make_client):
    refused = Completion(
        text="",
        usage=Usage(input_tokens=400, output_tokens=10),
        request_id="r1",
        model="fake-workhorse",
        stop_reason=StopReason.REFUSAL,
    )
    err = ContentFilteredError("provider refused")
    err.completion = refused
    client = make_client(FakeProvider([err]))
    with client.cost_scope() as scope, pytest.raises(ContentFilteredError) as info:
        await client.complete("something it refuses")
    assert info.value.cost is not None and info.value.cost.refused
    assert client.spend_usd > 0 and len(scope) == 1
    assert client.meter.refusal_count == 1 and client.meter.resilience()["refusals"] == 1


def test_the_meter_keeps_running_totals_and_bounded_records():
    meter = CostMeter(cap_usd=None, max_records=3)
    for i in range(5):
        meter.record(f"r{i}", "fake-workhorse", Usage(input_tokens=1000, output_tokens=0))
    assert len(meter.records) == 3 and meter.records[0].request_id == "r2"
    assert meter.total_usd == pytest.approx(5 * 1000 * 2.00 / 1e6)
    assert meter.total_usage.input_tokens == 5000
    assert meter.resilience()["completions"] == 5


# ----- one retry class per status across providers ----------------------------------------


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 529])
def test_every_provider_retries_the_same_statuses(status):
    import anthropic

    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"type": "x"}})
    a = ab.classify(anthropic.APIStatusError("boom", response=response, body=None))
    o = oc.classify_response(httpx.Response(status, request=request, json={}))
    exc = Exception("boom")
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "SomethingElse", "Message": "boom"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    b = bc.classify(exc, timeout_s=60)
    assert all(isinstance(r, RetryableError) for r in (a, o, b)), (a, o, b)


def test_model_unavailable_is_read_from_vendor_codes():
    import anthropic

    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    response = httpx.Response(400, request=request)
    body = {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}
    a = ab.classify(anthropic.BadRequestError("nope", response=response, body=body))
    assert isinstance(a, TerminalError) and a.code == "not_found_error" and a.model_unavailable

    o = oc.classify_response(
        httpx.Response(400, request=request, json={"error": {"code": "DeploymentNotFound"}})
    )
    assert o.code == "DeploymentNotFound" and o.model_unavailable
    g = oc.classify_response(
        httpx.Response(400, request=request, json=[{"error": {"status": "NOT_FOUND"}}])
    )
    assert g.model_unavailable
    ours = oc.classify_response(
        httpx.Response(400, request=request, json={"error": {"message": "bad model parameter"}})
    )
    assert not ours.model_unavailable, "a message mentioning the model is not a vendor code"


async def test_bedrock_runs_on_its_own_pool_sized_to_the_concurrency():
    class _Slow:
        def __init__(self):
            self.active = 0
            self.peak = 0

        def converse(self, **kwargs):
            import time

            self.active += 1
            self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            self.active -= 1
            return {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "usage": {"inputTokens": 1, "outputTokens": 1},
                "stopReason": "end_turn",
            }

    from nw.llm.types import Message

    fake = _Slow()
    provider = bc.BedrockConverseProvider(region="eu-central-1", client=fake, max_concurrency=12)
    assert provider.max_workers == 12
    await asyncio.gather(*(provider.complete([Message.user("x")], model="m") for _ in range(12)))
    assert fake.peak == 12, "every slot of the semaphore can be on the wire at once"
    await provider.aclose()


async def test_client_close_reaches_the_provider(make_client):
    closed = []

    class Closable(FakeProvider):
        async def aclose(self):
            closed.append(True)

    client = make_client(Closable())
    await client.aclose()
    assert closed == [True]
