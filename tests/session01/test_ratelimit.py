"""A token bucket per key id in front of every service: 429 with Retry-After beyond the
burst, refilled at the configured rate, probes exempt, counted per key id."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from nw.auth import install_api_key
from nw.ratelimit import (
    DEFAULT_BURST,
    DEFAULT_RPS,
    TokenBuckets,
    install_rate_limit,
    settings_from_env,
)

pytestmark = pytest.mark.session01


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limited(key_id: str) -> float:
    return REGISTRY.get_sample_value("nw_rate_limited_total", {"key_id": key_id}) or 0.0


def make(key: str, clock: Clock, rps=2.0, burst=3):
    app = FastAPI()
    # Registered first so it runs after the key check and sees the key id.
    buckets = install_rate_limit(app, rps=rps, burst=burst, clock=clock)
    install_api_key(app, key=key)

    @app.post("/work")
    def work():
        return {"did": "work"}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        return {"ok": True}

    return TestClient(app), buckets


def test_bucket_refills_at_the_rate_and_never_above_the_burst():
    clock = Clock()
    b = TokenBuckets(rps=2.0, burst=3, clock=clock)
    assert [b.take("k") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take("k") == pytest.approx(0.5), "half a second until the next token at 2 rps"
    clock.now += 0.5
    assert b.take("k") == 0.0
    clock.now += 60
    assert [b.take("k") for _ in range(4)][-1] > 0, "an hour idle still caps at the burst"
    assert b.take("other") == 0.0, "buckets are per subject"
    with pytest.raises(ValueError):
        TokenBuckets(rps=0, burst=3)


def test_oldest_subject_is_dropped_beyond_the_cap():
    b = TokenBuckets(rps=1, burst=1, max_subjects=2)
    for s in ("a", "b", "c"):
        b.take(s)
    assert len(b) == 2 and b.take("a") == 0.0, "a was forgotten and starts a fresh bucket"


def test_429_with_retry_after_per_key_id_and_probes_exempt():
    clock = Clock()
    keys = '{"cohort-a": "alpha-secret-1234", "ops": "ops-secret-5678"}'
    c, _ = make(keys, clock)
    a, o = {"x-api-key": "alpha-secret-1234"}, {"x-api-key": "ops-secret-5678"}
    before = _limited("cohort-a")
    assert [c.post("/work", headers=a).status_code for _ in range(3)] == [200, 200, 200]
    r = c.post("/work", headers=a)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    assert "NW_RATE_LIMIT" not in r.json()["detail"] and "retry in 1s" in r.json()["detail"]
    assert _limited("cohort-a") == before + 1
    assert c.post("/work", headers=o).status_code == 200, "another key, another bucket"
    assert c.get("/healthz").status_code == 200 and c.get("/metrics").status_code == 200
    assert c.post("/work").status_code == 401, "an unknown key is refused before any bucket"
    clock.now += 0.5
    assert c.post("/work", headers=a).status_code == 200, "one token back after half a second"


def test_without_a_key_the_bucket_is_per_client_address():
    clock = Clock()
    c, buckets = make("", clock)
    before = _limited("ip")
    for _ in range(3):
        assert c.post("/work").status_code == 200
    assert c.post("/work").status_code == 429
    assert _limited("ip") == before + 1, "the label is the constant `ip`, never the address"
    assert c.post("/work", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}).status_code == 200
    assert {s.split(":", 1)[0] for s in buckets._buckets} == {"ip"}


def test_defaults_and_off_switch(monkeypatch):
    monkeypatch.delenv("NW_RATE_LIMIT_RPS", raising=False)
    monkeypatch.delenv("NW_RATE_LIMIT_BURST", raising=False)
    assert settings_from_env() == (DEFAULT_RPS, DEFAULT_BURST) == (10.0, 30)
    monkeypatch.setenv("NW_RATE_LIMIT_RPS", "0")
    assert install_rate_limit(FastAPI()) is None
    monkeypatch.setenv("NW_RATE_LIMIT_RPS", "5")
    monkeypatch.setenv("NW_RATE_LIMIT_BURST", "7")
    assert settings_from_env() == (5.0, 7)
