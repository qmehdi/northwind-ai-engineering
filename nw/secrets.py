"""One way to read a secret by reference, on every track.

A deployed service never gets a secret in its image or its repository. It gets the value in the
environment (a laptop, or a platform that injects secrets as environment variables, which
Container Apps and Cloud Run both can) or a reference it resolves once at startup with its own
identity:

- `<PREFIX>_SECRET_ARN`: AWS Secrets Manager, read with the execution role.
- `<PREFIX>_SECRET_NAME`: Google Secret Manager, `projects/P/secrets/S/versions/latest`, read with
  the service account.
- `<PREFIX>_SECRET_URI`: Azure Key Vault, `https://<vault>.vault.azure.net/secrets/<name>[/<version>]`,
  read with the managed identity (`DefaultAzureCredential`) through the Key Vault REST API, so no
  Key Vault SDK is needed beside `azure-identity`.

`nw.auth` reads the API key (`NW_API_KEY`) and `nw.serving.gateway` the gateway key
(`NW_GATEWAY_KEY`) through `read_secret`; only the reference, never the value, is logged.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from nw.logging import get_logger, log_fields

log = get_logger("nw.secrets")

Fetch = Callable[[str], str]
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"
KEY_VAULT_API_VERSION = "7.4"


@dataclass(frozen=True)
class Secret:
    value: str
    source: str  # env, secrets-manager, secret-manager, key-vault, or none


def aws_secret(arn: str) -> str:
    import boto3

    region = (
        arn.split(":")[3]
        if arn.startswith("arn:")
        else os.environ.get("NW_AWS_REGION", "us-east-1")
    )
    return boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)[
        "SecretString"
    ]


def gcp_secret(name: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(request={"name": name}).payload.data.decode()


def key_vault_parts(uri: str) -> tuple[str, str, str]:
    """`(vault_url, name, version)` from a Key Vault secret URI; version may be empty."""
    parts = urlsplit(uri.strip())
    segments = [s for s in parts.path.split("/") if s]
    if (
        parts.scheme != "https"
        or not parts.netloc.endswith(".vault.azure.net")
        or len(segments) not in (2, 3)
        or segments[0] != "secrets"
    ):
        raise ValueError(f"not a Key Vault secret URI: {uri!r}")
    version = segments[2] if len(segments) == 3 else ""
    return f"https://{parts.netloc}", segments[1], version


def azure_secret(uri: str) -> str:
    import httpx
    from azure.identity import DefaultAzureCredential

    vault, name, version = key_vault_parts(uri)
    token = DefaultAzureCredential().get_token(KEY_VAULT_SCOPE).token
    path = f"{vault}/secrets/{name}" + (f"/{version}" if version else "")
    response = httpx.get(
        path,
        params={"api-version": KEY_VAULT_API_VERSION},
        headers={"authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["value"]


def read_secret(
    prefix: str,
    env: Mapping[str, str] | None = None,
    *,
    fetch_aws: Fetch = aws_secret,
    fetch_gcp: Fetch = gcp_secret,
    fetch_azure: Fetch = azure_secret,
) -> Secret:
    """The value of `<prefix>` from the environment, else from the first reference set, in the
    order ARN, NAME, URI. Nothing set is `Secret("", "none")`."""
    e = os.environ if env is None else env
    if e.get(prefix):
        return Secret(e[prefix], "env")
    references = (
        ("_SECRET_ARN", fetch_aws, "secrets-manager", lambda r: r.rsplit(":", 1)[0]),
        ("_SECRET_NAME", fetch_gcp, "secret-manager", lambda r: r.rsplit("/versions", 1)[0]),
        ("_SECRET_URI", fetch_azure, "key-vault", lambda r: r.split("?", 1)[0]),
    )
    for suffix, fetch, source, shown in references:
        ref = (e.get(prefix + suffix) or "").strip()
        if ref:
            value = fetch(ref)
            log.info(
                "secret loaded", extra=log_fields(secret=prefix, source=source, ref=shown(ref))
            )
            return Secret(value, source)
    return Secret("", "none")


__all__ = ["Secret", "aws_secret", "azure_secret", "gcp_secret", "key_vault_parts", "read_secret"]
