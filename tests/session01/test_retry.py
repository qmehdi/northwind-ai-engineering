"""Acceptance 2: retryable failures are retried with backoff, terminal ones are not."""

import pytest

from nw.llm.errors import RetryableError, TerminalError
from nw.llm.providers.fake import FakeProvider
from nw.llm.retry import RetryPolicy

pytestmark = pytest.mark.session01


async def test_two_429s_then_success(make_client, no_sleep):
    provider = FakeProvider(
        [
            RetryableError("rate limited", status=429),
            RetryableError("rate limited", status=429),
            "fine now",
        ]
    )
    client = make_client(provider)

    completion = await client.complete("hello")

    assert completion.text == "fine now"
    assert len(provider.calls) == 3
    assert len(no_sleep.delays) == 2, "one back-off per failed attempt"


async def test_400_fails_immediately_without_retry(make_client, no_sleep):
    provider = FakeProvider([TerminalError("bad request", status=400), "never reached"])
    client = make_client(provider)

    with pytest.raises(TerminalError):
        await client.complete("hello")

    assert len(provider.calls) == 1
    assert no_sleep.delays == []


async def test_gives_up_after_max_attempts(make_client, no_sleep):
    provider = FakeProvider([RetryableError("overloaded", status=529)])
    client = make_client(provider, retry=RetryPolicy(max_attempts=3, base_delay_s=0.01))

    with pytest.raises(RetryableError):
        await client.complete("hello")

    assert len(provider.calls) == 3
    assert len(no_sleep.delays) == 2


async def test_retry_after_header_is_honoured(make_client, no_sleep):
    provider = FakeProvider([RetryableError("rate limited", status=429, retry_after_s=2.5), "ok"])
    client = make_client(provider, retry=RetryPolicy(max_attempts=3, base_delay_s=0.01))

    await client.complete("hello")

    assert no_sleep.delays == [2.5]


def test_backoff_grows_and_is_capped_with_jitter():
    policy = RetryPolicy(max_attempts=6, base_delay_s=1.0, max_delay_s=4.0)
    for attempt, ceiling in [(1, 1.0), (2, 2.0), (3, 4.0), (5, 4.0)]:
        samples = [policy.delay_for(attempt) for _ in range(200)]
        assert all(0.0 <= s <= ceiling for s in samples), (attempt, max(samples))
        assert len(set(round(s, 6) for s in samples)) > 10, "no jitter"


def test_retry_after_is_capped():
    policy = RetryPolicy(retry_after_cap_s=30.0)
    assert policy.delay_for(1, retry_after_s=600.0) == 30.0
