"""API-key protection for every public service.

A public HTTPS endpoint that spends model tokens with no credential is an open
wallet. Every service accepts an `x-api-key` header checked against
`NW_API_KEY`, which arrives from the cloud's secret store at runtime and never
from an image or a repository. When the variable is unset (a laptop, the test
suite) the check is off and the log says so once.

Health endpoints stay open: the platform's probes carry no key. `/metrics`
stays open too, so a scraper in the same account can read it; it carries
counts, never customer text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nw.logging import get_logger, log_fields

log = get_logger("nw.auth")

OPEN_PATHS = {"/healthz", "/readyz", "/metrics"}


def service_client(timeout: float = 30) -> httpx.AsyncClient:
    """An HTTP client for one Northwind service calling another: carries the cohort API key
    when one is configured, and the caller's correlation ID on every request."""
    import httpx

    from nw.logging import correlation_id

    key = load_api_key()

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
    """The key comes from, in order: `NW_API_KEY` (a laptop, or a platform that injects
    secrets as environment), `NW_API_KEY_SECRET_ARN` (AWS Secrets Manager, read once at
    startup with the execution role), `NW_API_KEY_SECRET_NAME` (Google Secret Manager,
    `projects/P/secrets/S/versions/latest`, read once with the service account)."""
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


def install_api_key(app: FastAPI, *, key: str | None = None) -> None:
    key = load_api_key() if key is None else key
    if not key:
        log.warning("api key not set; endpoints are open", extra=log_fields(env="NW_API_KEY"))

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        if not key or request.url.path in OPEN_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        supplied = request.headers.get("x-api-key", "")
        # Constant-time comparison so a timing side channel cannot reveal the key.
        if not hmac.compare_digest(supplied.encode(), key.encode()):
            return JSONResponse({"detail": "missing or invalid x-api-key"}, status_code=401)
        return await call_next(request)
