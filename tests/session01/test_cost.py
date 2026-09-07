"""Acceptance 5: spend is queryable after a run, and the cap is a hard stop."""

import pytest

from nw.llm.cost import CostMeter
from nw.llm.errors import SpendCapExceeded
from nw.llm.providers.fake import FakeProvider
from nw.logging import bind_correlation_id

pytestmark = pytest.mark.session01


async def test_total_spend_and_usage_are_queryable(make_client):
    provider = FakeProvider(tokens_per_call=(1000, 500))
    client = make_client(provider)

    await client.map([f"q{i}" for i in range(4)])

    assert client.usage.input_tokens == 4000
    assert client.usage.output_tokens == 2000
    # fake-workhorse is priced like Sonnet 5: 4000 * 2.00 + 2000 * 10.00 per MTok
    assert client.spend_usd == pytest.approx((4000 * 2.00 + 2000 * 10.00) / 1e6)
    assert set(client.meter.by_model()) == {"fake-workhorse"}


async def test_spend_cap_stops_calls_before_they_are_made(make_client):
    provider = FakeProvider(tokens_per_call=(1000, 1000))
    meter = CostMeter(cap_usd=0.02)
    client = make_client(provider, meter=meter, max_concurrency=1)

    await client.complete("one", max_tokens=100)
    with pytest.raises(SpendCapExceeded):
        for _ in range(50):
            await client.complete("more", max_tokens=100)

    assert meter.total_usd <= 0.02 + 0.012, "cap overshoot beyond one call's cost"


async def test_correlation_id_is_recorded_on_every_call(make_client):
    provider = FakeProvider()
    client = make_client(provider)
    with bind_correlation_id("req-abc"):
        await client.complete("hello")
    assert client.meter.records[0].correlation_id == "req-abc"
