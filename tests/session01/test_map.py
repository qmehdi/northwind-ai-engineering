"""Acceptance 4: map preserves input order when replies arrive out of order."""

import pytest

from nw.llm.errors import TerminalError
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session01


async def test_order_is_preserved_when_replies_return_out_of_order(make_client):
    # Later prompts answer sooner: the first prompt waits longest.
    provider = FakeProvider(delay_s=lambda i: 0.02 * (10 - i))
    client = make_client(provider, max_concurrency=10)

    results = await client.map([f"q{i}" for i in range(10)])

    assert [r.text for r in results] == [f"echo:q{i}" for i in range(10)]


async def test_map_raises_first_failure_by_default(make_client):
    provider = FakeProvider(["a", TerminalError("bad"), "c"])
    client = make_client(provider, max_concurrency=1)
    with pytest.raises(TerminalError):
        await client.map(["1", "2", "3"])


async def test_map_can_return_exceptions_in_place(make_client):
    provider = FakeProvider(["a", TerminalError("bad"), "c"])
    client = make_client(provider, max_concurrency=1)
    results = await client.map(["1", "2", "3"], return_exceptions=True)
    assert results[0].text == "a"
    assert isinstance(results[1], TerminalError)
    assert results[2].text == "c"
