"""API-key protection for every public service.

A public HTTPS endpoint that spends model tokens with no credential is an open
wallet. Every service accepts an `x-api-key` header checked against
`NW_API_KEY`, which arrives from the cloud's secret store at runtime and never
from an image or a repository. When the variable is unset (a laptop, the test
suite) the check is off and the log says so once.

`NW_API_KEY` holds either one secret or a JSON object of key id to secret,
`{"cohort-a": "...", "ops": "..."}`, from the environment or from the cloud
secret the same way. The id is what leaves the service: every request line logs
`api_key_id`, `nw_requests_by_key_total{key_id}` counts by it, and the rate
limiter buckets by it. The secret itself is never logged and never labels a
metric. One secret is the id `default`.

Health endpoints stay open: the platform's probes carry no key. `/metrics`
stays open too, so a scraper in the same account can read it; it carries
counts, never customer text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

import hmac
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from nw.logging import get_logger, log_fields

log = get_logger("nw.auth")

OPEN_PATHS = {"/healthz", "/readyz", "/metrics"}
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
    """The raw value, one secret or a JSON key map, from, in order: `NW_API_KEY` (a laptop,
    or a platform that injects secrets as environment), `NW_API_KEY_SECRET_ARN` (AWS Secrets
    Manager, read once at startup with the execution role), `NW_API_KEY_SECRET_NAME` (Google
    Secret Manager, `projects/P/secrets/S/versions/latest`, read once with the service
    account)."""
    if os.environ.get("NW_API_KEY"):
        return os.environ["NW_API_KEY"]
    arn = os.environ.get("NW_API_KEY_SECRET_ARN")
    if arn:
        import boto3

        region = (
            arn.split(":")[3]
            if arn.startswith("arn:")
            else os.environ.get("NW_AWS_REGION", "us-east-1")
        )
        value = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)[
            "SecretString"
        ]
        log.info("api key loaded from secrets manager", extra=log_fields(arn=arn.rsplit(":", 1)[0]))
        return value
    name = os.environ.get("NW_API_KEY_SECRET_NAME")
    if name:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        value = client.access_secret_version(request={"name": name}).payload.data.decode()
        log.info(
            "api key loaded from secret manager",
            extra=log_fields(name=name.rsplit("/versions", 1)[0]),
        )
        return value
    return ""


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


def install_api_key(app: FastAPI, *, key: str | None = None) -> None:
    """The key check, one access line per request with the key id, and the per-key
    counter. `key` overrides the environment (tests); it takes the same shapes."""
    keys = parse_api_keys(load_api_key() if key is None else key)
    if not keys:
        log.warning("api key not set; endpoints are open", extra=log_fields(env="NW_API_KEY"))
    else:
        log.info("api keys loaded", extra=log_fields(key_ids=sorted(keys)))

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        if request.url.path in OPEN_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        key_id: str | None = None
        if keys:
            key_id = resolve_key_id(request.headers.get("x-api-key", ""), keys)
            if key_id is None:
                REQUESTS_BY_KEY.labels(key_id=REJECTED_KEY_ID).inc()
                log.warning(
                    "request",
                    extra=log_fields(
                        method=request.method,
                        path=request.url.path,
                        status=401,
                        api_key_id=None,
                    ),
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
                path=request.url.path,
                status=response.status_code,
                api_key_id=key_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            ),
        )
        return response
