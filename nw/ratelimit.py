"""A token bucket per API key in the service, in front of every model call.

The cohort shares one deployment and one bill. A loop in one participant's notebook must
not empty the spend cap for everyone, and an unauthenticated client hammering `/ask`
must not turn the cold-start budget into a bill. There is no API gateway in front of the
Session path (see docs/adr/0002), so the limit lives here, next to the key check.

One bucket per key id (`request.state.api_key_id`, set by `nw.auth`), or per client IP
when the service runs without a key. Each bucket holds `NW_RATE_LIMIT_BURST` tokens
(default 30) and refills at `NW_RATE_LIMIT_RPS` (default 10). A request takes one
token; with none left it gets 429 and a `Retry-After` in whole seconds. Probes and
`/metrics` are exempt: the platform never waits. `NW_RATE_LIMIT_RPS=0` turns it off.

The buckets are per process. Two Lambda instances or two Cloud Run instances each allow
the full rate, which is the accepted imprecision of doing this in the service: the cap
still holds within a small factor, and the spend cap in `nw.llm` is the hard stop.
"""

from __future__ import annotations

import math
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from nw.auth import OPEN_PATHS
from nw.logging import get_logger, log_fields

log = get_logger("nw.ratelimit")

DEFAULT_RPS = 10.0
DEFAULT_BURST = 30
MAX_SUBJECTS = 10_000  # buckets kept before the least recently seen is dropped

RATE_LIMITED = Counter("nw_rate_limited_total", "Requests refused by the rate limiter", ["key_id"])


@dataclass
class Bucket:
    tokens: float
    updated: float


class TokenBuckets:
    """Token buckets keyed by subject. `take` returns 0.0 when a token was spent, else the
    seconds until the next one, so the caller can set `Retry-After` honestly."""

    def __init__(
        self,
        rps: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_subjects: int = MAX_SUBJECTS,
    ) -> None:
        if rps <= 0 or burst < 1:
            raise ValueError("rps must be positive and burst at least 1")
        self.rps = float(rps)
        self.burst = int(burst)
        self._clock = clock
        self._max = max_subjects
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()

    def take(self, subject: str) -> float:
        now = self._clock()
        bucket = self._buckets.get(subject)
        if bucket is None:
            bucket = Bucket(tokens=float(self.burst), updated=now)
            self._buckets[subject] = bucket
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        else:
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rps)
            bucket.updated = now
            self._buckets.move_to_end(subject)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self.rps

    def __len__(self) -> int:
        return len(self._buckets)


def subject_of(request: Request) -> tuple[str, str]:
    """(bucket subject, metric label). The key id when the request carried a known key,
    else the client address. The metric label for an address is the constant `ip`: the
    counter must not grow one series per client."""
    key_id = getattr(request.state, "api_key_id", None)
    if key_id:
        return f"key:{key_id}", key_id
    forwarded = request.headers.get("x-forwarded-for", "")
    host = forwarded.split(",")[0].strip() if forwarded else ""
    if not host:
        host = request.client.host if request.client else "unknown"
    return f"ip:{host}", "ip"


def settings_from_env() -> tuple[float, int]:
    return (
        float(os.environ.get("NW_RATE_LIMIT_RPS", str(DEFAULT_RPS))),
        int(os.environ.get("NW_RATE_LIMIT_BURST", str(DEFAULT_BURST))),
    )


def install_rate_limit(
    app: FastAPI,
    *,
    rps: float | None = None,
    burst: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    exempt: frozenset[str] | set[str] = OPEN_PATHS,
) -> TokenBuckets | None:
    """Add the limiter to `app`. Register it before `install_api_key` so it runs after the
    key check and sees the key id (Starlette runs the last-added middleware first).
    Returns the buckets, or None when the limiter is off."""
    env_rps, env_burst = settings_from_env()
    rps = env_rps if rps is None else rps
    burst = env_burst if burst is None else burst
    if rps <= 0:
        log.info("rate limiting off", extra=log_fields(env="NW_RATE_LIMIT_RPS"))
        return None
    buckets = TokenBuckets(rps, burst, clock=clock)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path in exempt or request.method == "OPTIONS":
            return await call_next(request)
        subject, label = subject_of(request)
        wait = buckets.take(subject)
        if wait > 0:
            retry = max(1, math.ceil(wait))
            RATE_LIMITED.labels(key_id=label).inc()
            log.warning(
                "rate limited",
                extra=log_fields(
                    api_key_id=label if label != "ip" else None,
                    path=request.url.path,
                    retry_after_s=retry,
                ),
            )
            return JSONResponse(
                {
                    "detail": (
                        f"rate limit exceeded: {rps:g} requests per second with a burst of "
                        f"{burst}; retry in {retry}s"
                    )
                },
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)

    log.info("rate limiting on", extra=log_fields(rps=rps, burst=burst))
    return buckets
