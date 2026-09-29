from __future__ import annotations

import os

import pytest

# The suite never reaches a network: every role resolves to the fake provider and the
# `fake-*` model ids unless a test sets `provider` itself.
os.environ.setdefault("NW_PROVIDER", "fake")
# A checkout that has run `make deploy-azure` has deploy/azure/outputs.json; Settings would
# read the APIM gateway from it. The suite reads no deployment.
os.environ["NW_AZURE_OUTPUTS"] = ""

from nw.config import Settings, Track
from nw.llm.client import LLMClient
from nw.llm.cost import CostMeter
from nw.llm.providers.fake import FakeProvider
from nw.llm.retry import RetryPolicy


@pytest.fixture
def local_settings() -> Settings:
    return Settings(track=Track.LOCAL, max_concurrency=8, spend_cap_usd=100.0, _env_file=None)


@pytest.fixture
def no_sleep():
    """A sleep that records delays instead of waiting."""
    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    _sleep.delays = delays  # type: ignore[attr-defined]
    return _sleep


@pytest.fixture
def make_client(local_settings, no_sleep):
    def _make(provider: FakeProvider, **kw) -> LLMClient:
        kw.setdefault("settings", local_settings)
        kw.setdefault("sleep", no_sleep)
        kw.setdefault("retry", RetryPolicy(max_attempts=4, base_delay_s=0.01, max_delay_s=0.05))
        kw.setdefault("meter", CostMeter(cap_usd=local_settings.spend_cap_usd))
        return LLMClient(provider, **kw)

    return _make
