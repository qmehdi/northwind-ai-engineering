"""Acceptance 1: 100 prompts at max_concurrency=8 never exceed 8 in flight."""

import pytest

from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session01


async def test_semaphore_bounds_in_flight_requests(make_client):
    provider = FakeProvider(delay_s=0.005)
    client = make_client(provider, max_concurrency=8)

    results = await client.map([f"prompt {i}" for i in range(100)])

    assert len(results) == 100
    assert provider.high_water <= 8, f"high-water mark was {provider.high_water}"
    assert provider.high_water >= 2, "concurrency never happened at all"


async def test_concurrency_limit_is_configurable(make_client):
    provider = FakeProvider(delay_s=0.005)
    client = make_client(provider, max_concurrency=3)
    await client.map([f"p{i}" for i in range(20)])
    assert provider.high_water <= 3
