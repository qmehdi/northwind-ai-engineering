"""Resilience in the service layer: a fallback model per role, a circuit breaker per model,
the per-call timeout as a retryable failure, a token budget on a fan-out, and the settings
that govern all of it described with their sources."""

from __future__ import annotations

import subprocess
import sys

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nw import telemetry
from nw.config import ModelRole, Settings, Track, format_describe
from nw.llm.breaker import CircuitBreaker
from nw.llm.errors import (
    CircuitOpenError,
    ContentFilteredError,
    RequestTimeout,
    RetryableError,
    TerminalError,
    TokenBudgetExceeded,
)
from nw.llm.providers import anthropic_base as ab
from nw.llm.providers.fake import FakeProvider
from nw.llm.retry import RetryPolicy

pytestmark = pytest.mark.session01

PRIMARY, BACKUP = "fake-workhorse", "fake-backup"


@pytest.fixture
def fallback_settings() -> Settings:
    return Settings(
        track=Track.LOCAL,
        model_fallback_workhorse=BACKUP,
        spend_cap_usd=100.0,
        _env_file=None,
    )


def _primary_fails(error: Exception):
    """A script that fails the primary model every time and answers from the backup."""
    return lambda msgs, kw: error if kw["model"] == PRIMARY else f"{kw['model']} answered"


def _calls_to(provider: FakeProvider, model: str) -> int:
    return sum(1 for c in provider.calls if c["model"] == model)


# ----- fallback ---------------------------------------------------------------------


async def test_terminal_404_falls_back_once_and_marks_the_record(make_client, fallback_settings):
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter=exporter, env={})
    provider = FakeProvider(_primary_fails(TerminalError("model not found", status=404)))
    client = make_client(provider, settings=fallback_settings)

    completion = await client.complete("hello")

    assert completion.text == f"{BACKUP} answered"
    assert _calls_to(provider, PRIMARY) == 1 and _calls_to(provider, BACKUP) == 1
    rec = client.meter.records[0]
    assert rec.model == BACKUP and rec.fallback is True and rec.role == "workhorse"
    assert client.meter.fallback_count == 1 and client.meter.resilience()["fallbacks"] == 1
    telemetry.trace.get_tracer_provider().force_flush()
    sp = next(s for s in exporter.get_finished_spans() if s.name == "llm.complete")
    assert sp.attributes["nw.fallback"] is True
    assert sp.attributes["gen_ai.request.model"] == BACKUP


async def test_exhausted_429s_fall_back_and_count_the_attempts(
    make_client, fallback_settings, no_sleep
):
    provider = FakeProvider(_primary_fails(RetryableError("rate limited", status=429)))
    client = make_client(
        provider, settings=fallback_settings, retry=RetryPolicy(max_attempts=3, base_delay_s=0.01)
    )

    completion = await client.complete("hello")

    assert completion.text == f"{BACKUP} answered"
    assert _calls_to(provider, PRIMARY) == 3, "every retry on the primary before the fallback"
    assert len(no_sleep.delays) == 2
    rec = client.meter.records[0]
    assert rec.fallback is True and rec.attempts == 4  # three on the primary, one on the backup
    assert client.meter.retry_count == 3


async def test_model_unavailable_code_falls_back_but_a_refusal_does_not(
    make_client, fallback_settings
):
    unavailable = TerminalError("not deployed", status=400, code="DeploymentNotFound")
    provider = FakeProvider(_primary_fails(unavailable))
    client = make_client(provider, settings=fallback_settings)
    assert (await client.complete("hello")).text == f"{BACKUP} answered"

    # The vendor code decides, not the wording: our own 400 that mentions a model stays ours.
    ours = TerminalError("the model rejected the parameter temperature", status=400)
    provider = FakeProvider(_primary_fails(ours))
    client = make_client(provider, settings=fallback_settings)
    with pytest.raises(TerminalError):
        await client.complete("hello")
    assert _calls_to(provider, BACKUP) == 0

    provider = FakeProvider(_primary_fails(ContentFilteredError("refused")))
    client = make_client(provider, settings=fallback_settings)
    with pytest.raises(ContentFilteredError):
        await client.complete("hello")
    assert _calls_to(provider, BACKUP) == 0, "a refusal is about the content, not the model"

    provider = FakeProvider(_primary_fails(TerminalError("invalid request", status=400)))
    client = make_client(provider, settings=fallback_settings)
    with pytest.raises(TerminalError):
        await client.complete("hello")
    assert _calls_to(provider, BACKUP) == 0, "our bad request would be bad on the backup too"


async def test_without_a_fallback_the_primary_error_is_raised(make_client):
    provider = FakeProvider(_primary_fails(TerminalError("model not found", status=404)))
    client = make_client(provider)
    with pytest.raises(TerminalError):
        await client.complete("hello")
    assert client.meter.records == []


def test_fallback_equal_to_the_primary_is_no_fallback():
    s = Settings(track=Track.LOCAL, model_fallback_workhorse=PRIMARY, _env_file=None)
    assert s.fallback_for(ModelRole.WORKHORSE) is None and s.fallback == {}
    s = Settings(track=Track.LOCAL, model_fallback_judge="fake-backup-judge", _env_file=None)
    assert s.fallback == {ModelRole.JUDGE: "fake-backup-judge"}


# ----- circuit breaker --------------------------------------------------------------


def test_breaker_opens_after_n_failures_and_half_opens_after_t_seconds():
    now = [100.0]
    b = CircuitBreaker(threshold=2, open_s=10.0, clock=lambda: now[0])
    assert b.allow("m") and b.state("m") == "closed"
    b.failure("m")
    assert b.allow("m"), "one failure is not a pattern"
    b.failure("m")
    assert not b.allow("m") and b.state("m") == "open"
    now[0] += 9.9
    assert not b.allow("m")
    now[0] += 0.2
    assert b.state("m") == "half_open"
    assert b.allow("m") and not b.allow("m"), "half open lets exactly one probe through"
    b.failure("m")
    assert b.state("m") == "open", "a failed probe opens it again for another window"
    now[0] += 10.0
    assert b.allow("m")
    b.success("m")
    assert b.state("m") == "closed" and b.allow("m") and b.allow("m")
    assert b.snapshot()["m"] == {"state": "closed", "consecutive_failures": 0}


async def test_open_circuit_skips_the_dead_model_without_paying_the_retries(
    make_client, fallback_settings, no_sleep
):
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, open_s=30.0, clock=lambda: now[0])
    provider = FakeProvider(_primary_fails(RetryableError("overloaded", status=529)))
    client = make_client(
        provider,
        settings=fallback_settings,
        breaker=breaker,
        retry=RetryPolicy(max_attempts=3, base_delay_s=0.01),
    )

    for _ in range(2):
        assert (await client.complete("q")).text == f"{BACKUP} answered"
    assert _calls_to(provider, PRIMARY) == 6 and breaker.state(PRIMARY) == "open"

    assert (await client.complete("q")).text == f"{BACKUP} answered"
    assert _calls_to(provider, PRIMARY) == 6, "skipped: no call, no retries, no back-off"
    rec = client.meter.records[-1]
    assert rec.skipped == (PRIMARY,) and rec.fallback is True and rec.attempts == 1
    assert client.meter.resilience()["breaker_skips"] == {PRIMARY: 1}

    now[0] += 31.0
    assert (await client.complete("q")).text == f"{BACKUP} answered"
    assert _calls_to(provider, PRIMARY) == 9, "half open: one probe, with its retries"
    assert breaker.state(PRIMARY) == "open"


async def test_every_circuit_open_raises_without_a_call(make_client):
    breaker = CircuitBreaker(threshold=1, open_s=60.0)
    provider = FakeProvider([TerminalError("model not found", status=404), "never"])
    client = make_client(provider, breaker=breaker)
    with pytest.raises(TerminalError):
        await client.complete("q")
    with pytest.raises(CircuitOpenError) as info:
        await client.complete("q")
    assert info.value.models == [PRIMARY] and len(provider.calls) == 1


async def test_a_success_closes_the_circuit(make_client, no_sleep):
    breaker = CircuitBreaker(threshold=3, open_s=60.0)
    provider = FakeProvider(
        [TerminalError("model not found", status=404), "fine", "fine"], tokens_per_call=(10, 10)
    )
    client = make_client(provider, breaker=breaker)
    with pytest.raises(TerminalError):
        await client.complete("q")
    assert breaker.snapshot()[PRIMARY]["consecutive_failures"] == 1
    await client.complete("q")
    assert breaker.snapshot()[PRIMARY]["consecutive_failures"] == 0


# ----- timeouts ---------------------------------------------------------------------


async def test_a_slow_provider_is_a_retryable_timeout(make_client, no_sleep):
    provider = FakeProvider(delay_s=lambda i: 2.0 if i == 0 else 0.0)
    client = make_client(provider, timeout_s=0.05)

    completion = await client.complete("hello")

    assert completion.text == "echo:hello" and len(provider.calls) == 2
    assert client.meter.records[0].attempts == 2 and provider.in_flight == 0


async def test_timeouts_give_up_after_the_retry_policy(make_client, no_sleep):
    provider = FakeProvider(delay_s=2.0)
    client = make_client(provider, timeout_s=0.02, retry=RetryPolicy(max_attempts=2))
    with pytest.raises(RequestTimeout) as info:
        await client.complete("hello")
    assert info.value.timeout_s == 0.02 and isinstance(info.value, RetryableError)
    assert len(provider.calls) == 2


async def test_per_call_timeout_overrides_the_setting(make_client, no_sleep):
    provider = FakeProvider(delay_s=0.05)
    client = make_client(provider, timeout_s=0.01, retry=RetryPolicy(max_attempts=1))
    assert (await client.complete("hello", timeout_s=1.0)).text == "echo:hello"
    assert client.timeout_s == 0.01


def test_sdk_timeout_is_classified_as_a_request_timeout():
    import anthropic

    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    request.extensions["timeout"] = {"connect": 5.0, "read": 60.0, "write": 5.0, "pool": 5.0}
    err = ab.classify(anthropic.APITimeoutError(request=request))
    assert isinstance(err, RequestTimeout) and err.timeout_s == 60.0


# ----- token budget -----------------------------------------------------------------


async def test_map_stops_sending_when_the_token_budget_is_used_up(make_client):
    provider = FakeProvider(tokens_per_call=(100, 50))
    client = make_client(provider, max_concurrency=1)

    results = await client.map(["a", "b", "c", "d"], max_total_tokens=300, return_exceptions=True)

    assert [type(r).__name__ for r in results] == [
        "Completion",
        "Completion",
        "TokenBudgetExceeded",
        "TokenBudgetExceeded",
    ]
    assert len(provider.calls) == 2, "the prompts over budget were never sent"
    with pytest.raises(TokenBudgetExceeded):
        await client.map(["a", "b", "c"], max_total_tokens=150)


# ----- configuration governance -----------------------------------------------------


def test_describe_names_the_source_of_every_setting(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NW_LOG_FORMAT=text\n")
    s = Settings(max_concurrency=3, api_key="hunter2", _env_file=env_file)
    rows = {
        r["name"]: r
        for r in s.describe(env={"NW_TRACK": "gcp", "NW_API_KEY": "hunter2"}, env_file=env_file)
    }
    assert rows["track"]["source"] == "env" and rows["track"]["value"] == "local"
    assert rows["log_format"]["source"] == ".env" and rows["log_format"]["value"] == "text"
    assert rows["max_concurrency"]["source"] == "init" and rows["max_concurrency"]["value"] == 3
    assert rows["spend_cap_usd"]["source"] == "default"
    assert rows["api_key"] == {
        "name": "api_key",
        "env": "NW_API_KEY",
        "value": "set",
        "source": "env",
        "secret": True,
    }
    text = format_describe(s.describe(env={}, env_file=env_file), s.config_hash())
    assert "hunter2" not in text and "config_hash" in text and "NW_MODEL_FALLBACK_JUDGE" in text


def test_config_hash_covers_settings_and_never_secrets():
    base = Settings(track=Track.LOCAL, _env_file=None)
    assert len(base.config_hash()) == 12 and base.config_hash() == base.config_hash()
    assert Settings(track=Track.LOCAL, max_concurrency=2, _env_file=None).config_hash() != (
        base.config_hash()
    )
    assert Settings(track=Track.LOCAL, api_key="x", _env_file=None).config_hash() == (
        base.config_hash()
    )
    assert (
        Settings(track=Track.LOCAL, model_fallback_workhorse=BACKUP, _env_file=None).config_hash()
        != base.config_hash()
    )


def test_python_m_nw_config_prints_the_table():
    out = subprocess.run(
        [sys.executable, "-m", "nw.config"], capture_output=True, text=True, check=True
    ).stdout
    assert "NW_TRACK" in out and "NW_REQUEST_TIMEOUT_S" in out and "config_hash" in out
