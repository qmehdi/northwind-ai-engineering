"""Small hardening in the shared layer: correlation IDs from callers are validated, the Lambda
span flush runs off the event loop, connection strings are secrets, Hub models are pinned."""

from __future__ import annotations

import asyncio
import threading

import pytest

from nw import telemetry
from nw.config import HF_REVISIONS, Settings, Track, hf_revision, is_secret
from nw.logging import bind_correlation_id, correlation_id

pytestmark = pytest.mark.session01


@pytest.mark.parametrize(
    "value",
    ["abc\nlevel=ERROR", "../../etc/passwd", "a" * 200, "x y", '{"inject": 1}', ""],
)
def test_a_malformed_correlation_id_is_replaced(value):
    with bind_correlation_id(value) as cid:
        assert cid != value and correlation_id() == cid and len(cid) == 16


def test_a_well_formed_correlation_id_is_kept():
    for value in ("req-abc", "0f3a9c1d2e4b5a6c", "trace:1.2_3"):
        with bind_correlation_id(value) as cid:
            assert cid == value


async def test_span_flush_runs_in_a_worker_thread(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    seen: list[bool] = []

    class Provider(TracerProvider):
        def force_flush(self, timeout_millis: int = 30000) -> bool:
            seen.append(threading.current_thread() is threading.main_thread())
            return True

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: Provider())
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(3):
            ticks += 1
            await asyncio.sleep(0)

    assert await asyncio.gather(telemetry.flush_off_loop(), ticker())
    assert seen == [False], "force_flush ran on the event loop's thread"


def test_connection_strings_are_redacted():
    assert is_secret("azure_appinsights_connection_string")
    s = Settings(
        track=Track.AZURE,
        azure_appinsights_connection_string="InstrumentationKey=00000000-dead-beef",
        _env_file=None,
    )
    row = next(r for r in s.describe(env={}) if r["name"] == "azure_appinsights_connection_string")
    assert row["secret"] and row["value"] == "set"
    assert "dead-beef" not in str(s.describe(env={})) and s.config_hash()


def test_hub_models_are_pinned(monkeypatch, tmp_path):
    assert (
        hf_revision("sentence-transformers/all-MiniLM-L6-v2")
        == HF_REVISIONS["sentence-transformers/all-MiniLM-L6-v2"]
    )
    assert hf_revision("cross-encoder/ms-marco-MiniLM-L-6-v2"), "the old name is an alias"
    assert hf_revision(str(tmp_path)) is None, "a local directory needs no revision"
    with pytest.raises(ValueError, match="HF_REVISIONS"):
        hf_revision("someone/unpinned")
    monkeypatch.setenv("NW_HF_REVISION_SOMEONE_UNPINNED", "deadbeef")
    assert hf_revision("someone/unpinned") == "deadbeef"
