"""A token bucket per API key in the service, in front of every model call.

The cohort shares one deployment and one bill. A loop in one participant's notebook must
not empty the spend cap for everyone, and an unauthenticated client hammering `/ask`
must not turn the cold-start budget into a bill. The services are reached directly, not through
an API gateway (see docs/SECURITY.md), so the limit lives here, next to the key check.

One bucket per key id (`request.state.api_key_id`, set by `nw.auth`), or per client address
when the request carries no key (a service running open, the platform-authenticated predict
route). The address is the TCP peer, or with `NW_TRUSTED_PROXY_HOPS=N` the N-th address from
the right of `X-Forwarded-For`: the one the outermost proxy you run appended, which a client
cannot forge. The leftmost entry is whatever the client sent and is never trusted.

Each bucket holds `NW_RATE_LIMIT_BURST` tokens (default 30) and refills at `NW_RATE_LIMIT_RPS`
(default 10). A request takes one token; with none left it gets 429 and a `Retry-After` in
whole seconds. Probes and `/metrics` are exempt: the platform never waits.
`NW_RATE_LIMIT_RPS=0` turns it off.

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

from nw.logging import get_logger, log_fields

log = get_logger("nw.ratelimit")

# Probes and the scraper, never limited; `exempt_paths(app)` is the app's own copy.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
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

    def reset(self) -> None:
        """Forget every subject: each starts again with a full bucket (tests, an operator)."""
        self._buckets.clear()

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


def trusted_hops(env: dict[str, str] | None = None) -> int:
    e = os.environ if env is None else env
    try:
        return max(0, int(e.get("NW_TRUSTED_PROXY_HOPS") or 0))
    except ValueError:
        return 0


def client_address(request: Request, hops: int | None = None) -> str:
    """The client address to bucket by. With `hops` trusted proxies in front (a load balancer,
    Cloud Run's front end), the address the outermost of them appended to `X-Forwarded-For`;
    with none, the TCP peer. A header shorter than the trusted chain falls back to the peer."""
    hops = trusted_hops() if hops is None else hops
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    chain = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return chain[-hops] if len(chain) >= hops else peer


def exempt_paths(app: FastAPI) -> set[str]:
    """The paths this app never limits: the probes, `/metrics`, and what the app added."""
    paths = getattr(app.state, "nw_rate_exempt", None)
    if paths is None:
        paths = set(EXEMPT_PATHS)
        app.state.nw_rate_exempt = paths
    return paths


def subject_of(request: Request) -> tuple[str, str]:
    """(bucket subject, metric label). The key id when the request carried a known key,
    else the client address. The metric label for an address is the constant `ip`: the
    counter must not grow one series per client."""
    key_id = getattr(request.state, "api_key_id", None)
    if key_id:
        return f"key:{key_id}", key_id
    return f"ip:{client_address(request)}", "ip"


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
    exempt: frozenset[str] | set[str] | None = None,
    only: frozenset[str] | set[str] | None = None,
) -> TokenBuckets | None:
    """Add the limiter to `app`. Register it before `install_api_key` so it runs after the
    key check and sees the key id (Starlette runs the last-added middleware first).
    `exempt` adds paths to the app's exempt set; `only` limits the limiter to those paths.
    Returns the buckets, or None when the limiter is off."""
    env_rps, env_burst = settings_from_env()
    rps = env_rps if rps is None else rps
    burst = env_burst if burst is None else burst
    if rps <= 0:
        log.info("rate limiting off", extra=log_fields(env="NW_RATE_LIMIT_RPS"))
        return None
    buckets = TokenBuckets(rps, burst, clock=clock)
    # `exempt` is kept as given and asked with `in`, so a container with its own rule (every
    # path but a few) works as well as a set of paths.
    extra = exempt if exempt is not None else frozenset()
    scope = frozenset(only) if only is not None else None

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        path = request.url.path
        if scope is not None and path not in scope:
            return await call_next(request)
        if path in exempt_paths(app) or path in extra or request.method == "OPTIONS":
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
