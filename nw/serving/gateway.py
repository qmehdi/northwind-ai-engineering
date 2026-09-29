"""The model gateway key from the platform's secret store.

A deployed service gets `NW_GATEWAY_URL` and a reference to the tenant's virtual key, never
the key itself: `NW_GATEWAY_KEY_SECRET_ARN` (AWS Secrets Manager, read once at startup with the
execution role) or `NW_GATEWAY_KEY_SECRET_NAME` (Google Secret Manager,
`projects/P/secrets/S/versions/latest`, read once with the service account). The same shapes
`nw.auth` uses for the API key. `NW_GATEWAY_KEY` in the environment wins when set (a laptop,
or a platform that injects secrets as environment variables).

`resolve_gateway_key` returns settings with the key filled in, so a service builds its client
with `make_provider(resolve_gateway_key(settings()))` and every model call carries the tenant's
key as the bearer token the gateway attributes cost by.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from nw.config import Settings
from nw.logging import get_logger, log_fields

log = get_logger("nw.serving.gateway")

ARN_VAR = "NW_GATEWAY_KEY_SECRET_ARN"
NAME_VAR = "NW_GATEWAY_KEY_SECRET_NAME"


def _aws_secret(arn: str) -> str:
    import boto3

    region = (
        arn.split(":")[3]
        if arn.startswith("arn:")
        else os.environ.get("NW_AWS_REGION", "us-east-1")
    )
    return boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)[
        "SecretString"
    ]


def _gcp_secret(name: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(request={"name": name}).payload.data.decode()


def resolve_gateway_key(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    fetch_aws: Callable[[str], str] = _aws_secret,
    fetch_gcp: Callable[[str], str] = _gcp_secret,
) -> Settings:
    """Settings whose `gateway_key` is set from the secret store when the environment names
    one and no key is present. Without a gateway, or with a key already set, the same
    settings come back untouched."""
    e = os.environ if env is None else env
    if not settings.gateway_url or settings.gateway_key:
        return settings
    arn = (e.get(ARN_VAR) or "").strip()
    name = (e.get(NAME_VAR) or "").strip()
    if arn:
        value = fetch_aws(arn)
        log.info(
            "gateway key loaded from secrets manager", extra=log_fields(arn=arn.rsplit(":", 1)[0])
        )
    elif name:
        value = fetch_gcp(name)
        log.info(
            "gateway key loaded from secret manager",
            extra=log_fields(name=name.rsplit("/versions", 1)[0]),
        )
    else:
        log.warning(
            "gateway configured without a key",
            extra=log_fields(
                gateway_url=settings.gateway_url, env=f"NW_GATEWAY_KEY, {ARN_VAR}, {NAME_VAR}"
            ),
        )
        return settings
    return settings.model_copy(update={"gateway_key": value.strip()})


def gateway_fields(settings: Settings) -> dict[str, str]:
    """What `/version` says about the gateway: the URL or `direct`, and whether a key is set."""
    return {
        "gateway": settings.gateway_url or "direct",
        "gateway_key": "set" if settings.gateway_key else "unset",
    }
