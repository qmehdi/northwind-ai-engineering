"""The model gateway key from the platform's secret store.

A deployed service gets `NW_GATEWAY_URL` and a reference to the tenant's virtual key, never
the key itself: `NW_GATEWAY_KEY_SECRET_ARN` (AWS Secrets Manager, read once at startup with the
execution role), `NW_GATEWAY_KEY_SECRET_NAME` (Google Secret Manager,
`projects/P/secrets/S/versions/latest`, read once with the service account) or
`NW_GATEWAY_KEY_SECRET_URI` (Azure Key Vault, read with the managed identity), all through
`nw.secrets.read_secret`. The same shapes
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
from nw.secrets import aws_secret, azure_secret, gcp_secret, read_secret

log = get_logger("nw.serving.gateway")

PREFIX = "NW_GATEWAY_KEY"
ARN_VAR = f"{PREFIX}_SECRET_ARN"
NAME_VAR = f"{PREFIX}_SECRET_NAME"
URI_VAR = f"{PREFIX}_SECRET_URI"


def resolve_gateway_key(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    fetch_aws: Callable[[str], str] = aws_secret,
    fetch_gcp: Callable[[str], str] = gcp_secret,
    fetch_azure: Callable[[str], str] = azure_secret,
) -> Settings:
    """Settings whose `gateway_key` is set from the secret store when the environment names
    one and no key is present. Without a gateway, or with a key already set, the same
    settings come back untouched."""
    e = os.environ if env is None else env
    if not settings.gateway_url or settings.gateway_key:
        return settings
    # The key itself in the environment is already in `settings`; only references are read here.
    refs = {k: v for k, v in e.items() if k in (ARN_VAR, NAME_VAR, URI_VAR)}
    secret = read_secret(
        PREFIX, refs, fetch_aws=fetch_aws, fetch_gcp=fetch_gcp, fetch_azure=fetch_azure
    )
    if not secret.value:
        log.warning(
            "gateway configured without a key",
            extra=log_fields(
                gateway_url=settings.gateway_url,
                env=f"{PREFIX}, {ARN_VAR}, {NAME_VAR}, {URI_VAR}",
            ),
        )
        return settings
    return settings.model_copy(update={"gateway_key": secret.value.strip()})


def gateway_fields(settings: Settings) -> dict[str, str]:
    """What `/version` says about the gateway: the URL or `direct`, and whether a key is set."""
    return {
        "gateway": settings.gateway_url or "direct",
        "gateway_key": "set" if settings.gateway_key else "unset",
    }
