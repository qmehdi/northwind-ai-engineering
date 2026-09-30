"""API-key protection for every public service.

A public HTTPS endpoint that spends model tokens with no credential is an open
wallet. Every service accepts an `x-api-key` header checked against
`NW_API_KEY`, which arrives from the cloud's secret store at runtime and never
from an image or a repository (`nw.secrets`: an ARN, a Secret Manager name or a Key
Vault URI). Without a key the service fails closed: every request except the probes
gets 503. Only the Local track (`NW_TRACK` unset or `local`) or an explicit
`NW_AUTH_DISABLED=1` runs open, and the log says so once.

`NW_API_KEY` holds either one secret or a JSON object of key id to secret,
`{"cohort-a": "...", "ops": "..."}`, from the environment or from the cloud
secret the same way. The id is what leaves the service: every request line logs
`api_key_id`, `nw_requests_by_key_total{key_id}` counts by it, and the rate
limiter buckets by it. The secret itself is never logged and never labels a
metric. One secret is the id `default`.

Health endpoints stay open: the platform's probes carry no key. `/metrics`
stays open too, so a scraper in the same account can read it; it carries
counts, never customer text. The open paths are per app (`open_paths(app)`): a
route one service opens (the Agent Platform predict route under the platform)
opens nowhere else.

A wrong or missing key is answered 401, and failures are rate limited per client
address (`NW_AUTH_FAIL_RPS`, `NW_AUTH_FAIL_BURST`): a client guessing keys gets 429
long before it gets far.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

import hmac
import json
import math
import os
import time
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from nw.logging import get_logger, log_fields
from nw.secrets import read_secret

log = get_logger("nw.auth")

# The default open paths of every app; `open_paths(app)` is the app's own copy.
OPEN_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
DISABLED_VAR = "NW_AUTH_DISABLED"
AUTH_FAIL_RPS = 1.0  # failed key checks refilled per second, per client address
AUTH_FAIL_BURST = 10
DEFAULT_KEY_ID = "default"
REJECTED_KEY_ID = "rejected"
NO_KEY_ID = "none"

REQUESTS_BY_KEY = Counter(
    "nw_requests_by_key_total",
    "Requests by API key id; `rejected` failed the key check, `none` means no key configured",
    ["key_id"],
)


def service_client(timeout: float = 30) -> httpx.AsyncClient:
    """An HTTP client for one Northwind service calling another: carries the cohort API key
    when one is configured (the first entry of a key map), and the caller's correlation ID
    on every request."""
    import httpx

    from nw.logging import correlation_id

    keys = load_api_keys()
    key = next(iter(keys.values()), "")

    async def add_correlation(request: httpx.Request) -> None:
        cid = correlation_id()
        if cid:
            request.headers["x-correlation-id"] = cid

    return httpx.AsyncClient(
        timeout=timeout,
        headers={"x-api-key": key} if key else None,
        event_hooks={"request": [add_correlation]},
    )


def load_api_key() -> str:
    """The raw value, one secret or a JSON key map, from `NW_API_KEY`, else the reference in
    `NW_API_KEY_SECRET_ARN` (AWS), `NW_API_KEY_SECRET_NAME` (Google) or
    `NW_API_KEY_SECRET_URI` (Azure Key Vault), read once at startup (`nw.secrets`)."""
    return read_secret("NW_API_KEY").value


def may_run_open(env: Mapping[str, str] | None = None) -> bool:
    """Whether a service without a key may serve: on the Local track, or when the operator set
    `NW_AUTH_DISABLED=1` on purpose. Anywhere else no key means closed."""
    e = os.environ if env is None else env
    if (e.get(DISABLED_VAR) or "").strip().lower() in ("1", "true", "yes"):
        return True
    return (e.get("NW_TRACK") or "local").strip().lower() == "local"


def open_paths(app: FastAPI) -> set[str]:
    """The paths this app serves without a key: the probes and `/metrics`, plus what the app
    itself added (`nw.serving.vertex` adds the platform's health route)."""
    paths = getattr(app.state, "nw_open_paths", None)
    if paths is None:
        paths = set(OPEN_PATHS)
        app.state.nw_open_paths = paths
    return paths


def parse_api_keys(raw: str) -> dict[str, str]:
    """One secret becomes `{"default": secret}`; a JSON object is a key id to secret map.
    Empty means no key. Anything else is a configuration error and the service must not
    start open by accident, so it raises."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("NW_API_KEY looks like JSON but does not parse") from exc
        well_formed = isinstance(parsed, dict) and bool(parsed)
        if well_formed:
            well_formed = all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in parsed.items()
            )
        if not well_formed:
            raise ValueError("NW_API_KEY as JSON must be a non-empty object of key id to secret")
        return dict(parsed)
    return {DEFAULT_KEY_ID: raw}


def load_api_keys() -> dict[str, str]:
    return parse_api_keys(load_api_key())


def resolve_key_id(supplied: str, keys: dict[str, str]) -> str | None:
    """The id of the key `supplied` matches, or None. Every candidate is compared in
    constant time and the loop never exits early, so timing reveals neither the secret nor
    which id matched."""
    found: str | None = None
    for key_id, secret in keys.items():
        if hmac.compare_digest(supplied.encode(), secret.encode()):
            found = key_id
    return found


def install_api_key(
    app: FastAPI,
    *,
    key: str | None = None,
    only: frozenset[str] | set[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """The key check, one access line per request with the key id, and the per-key
    counter. `key` overrides the environment (tests); it takes the same shapes. `only`
    limits the check to those paths (an app that mounts a keyed app underneath protects its
    own routes and leaves the rest to the mounted app's check)."""
    keys = parse_api_keys(load_api_key() if key is None else key)
    closed = False
    if not keys:
        if may_run_open(env):
            log.warning("api key not set; endpoints are open", extra=log_fields(env="NW_API_KEY"))
        else:
            closed = True
            log.error(
                "api key not set off the local track; refusing every request",
                extra=log_fields(env="NW_API_KEY", override=f"{DISABLED_VAR}=1"),
            )
    else:
        log.info("api keys loaded", extra=log_fields(key_ids=sorted(keys)))
    from nw.ratelimit import TokenBuckets, client_address

    e = os.environ if env is None else env
    failures = TokenBuckets(
        float(e.get("NW_AUTH_FAIL_RPS") or AUTH_FAIL_RPS),
        int(e.get("NW_AUTH_FAIL_BURST") or AUTH_FAIL_BURST),
    )
    scope = frozenset(only) if only is not None else None

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        path = request.url.path
        if scope is not None and path not in scope:
            return await call_next(request)
        if path in open_paths(app) or request.method == "OPTIONS":
            return await call_next(request)
        if closed:
            REQUESTS_BY_KEY.labels(key_id=REJECTED_KEY_ID).inc()
            return JSONResponse(
                {"detail": f"no API key configured; set NW_API_KEY (or {DISABLED_VAR}=1)"},
                status_code=503,
            )
        key_id: str | None = None
        if keys:
            key_id = resolve_key_id(request.headers.get("x-api-key", ""), keys)
            if key_id is None:
                REQUESTS_BY_KEY.labels(key_id=REJECTED_KEY_ID).inc()
                wait = failures.take(client_address(request))
                status = 429 if wait > 0 else 401
                log.warning(
                    "request",
                    extra=log_fields(
                        method=request.method, path=path, status=status, api_key_id=None
                    ),
                )
                if status == 429:
                    retry = max(1, math.ceil(wait))
                    return JSONResponse(
                        {"detail": f"too many failed key checks; retry in {retry}s"},
                        status_code=429,
                        headers={"Retry-After": str(retry)},
                    )
                return JSONResponse({"detail": "missing or invalid x-api-key"}, status_code=401)
        request.state.api_key_id = key_id
        REQUESTS_BY_KEY.labels(key_id=key_id or NO_KEY_ID).inc()
        started = time.perf_counter()
        response = await call_next(request)
        log.info(
            "request",
            extra=log_fields(
                method=request.method,
                path=path,
                status=response.status_code,
                api_key_id=key_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            ),
        )
        return response


def protect(app: FastAPI, paths: frozenset[str] | set[str]) -> None:
    """Key check and rate limit on `paths` of `app` only: for an app whose other routes belong
    to a keyed app mounted underneath (the managed-runtime contract routes in
    `nw.agent.agentcore`), so no request is checked or counted twice."""
    from nw.ratelimit import install_rate_limit

    install_rate_limit(app, only=paths)
    install_api_key(app, only=paths)
