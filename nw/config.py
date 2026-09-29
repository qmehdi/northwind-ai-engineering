"""Settings for every service in the course.

One object, read once at startup. Code asks for a model by role, never by ID,
and never imports a cloud SDK directly: that is the provider's job.

    uv run python -m nw.config      # every effective setting, its source, the config hash

`describe()` says where each value came from (the environment, `.env`, or the default) with
secrets redacted, and `config_hash()` is twelve hex characters over the non-secret values,
so two instances answering `/version` can be told apart by configuration alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Track(StrEnum):
    AWS = "aws"
    GCP = "gcp"
    LOCAL = "local"


class ProviderMode(StrEnum):
    """`auto` picks the track's real providers; `fake` scripts every role in memory."""

    AUTO = "auto"
    FAKE = "fake"


class ModelRole(StrEnum):
    WORKHORSE = "workhorse"
    JUDGE = "judge"
    ECONOMY = "economy"


# Track defaults (ADR 0010: open-weight Workhorse and Economy, Claude as Judge). Verify
# these IDs against the provider's model list before each delivery: `make preflight` does
# one round trip per role and prints which provider and endpoint each role resolves to.
#
# Model ids, fetched 2026-09-29:
# - Bedrock (docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html, model cards):
#   gpt-oss-120b is `openai.gpt-oss-120b-1:0` on bedrock-runtime, Converse and tool use
#   supported, in-region in us-east-1, us-east-2, us-west-2 and the EU and APAC regions listed
#   on the card; the only geo profile is `us-gov.openai.gpt-oss-120b-1:0`, there is no `us.`
#   or `global.` profile. gpt-oss-20b is `openai.gpt-oss-20b-1:0`, same shape. Nova Micro is
#   `amazon.nova-micro-v1:0` with geo profiles `us.amazon.nova-micro-v1:0` and
#   `eu.amazon.nova-micro-v1:0`; the `us.` profile serves us-east-1, us-east-2 and us-west-2.
# - Google (docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/openai/...):
#   the Agent Platform (formerly Vertex AI) serves the managed API with model id
#   `gpt-oss-120b-maas` (regions `global` and `us-central1`) and `gpt-oss-20b-maas`
#   (`us-central1` only; deprecated 2026-07-21, retirement announced for 2026-10-21). Requests
#   name the model `openai/<id>` on the OpenAI-compatible chat completions endpoint
#   `https://<region>-aiplatform.googleapis.com/v1/projects/<project>/locations/<region>/endpoints/openapi/chat/completions`.
# - Ollama (ollama.com/library/gpt-oss): tags `gpt-oss:20b` (14 GB) and `gpt-oss:120b`
#   (65 GB), both with the `tools` and `thinking` capabilities.
DEFAULT_MODELS: dict[Track, dict[ModelRole, str]] = {
    Track.AWS: {
        ModelRole.WORKHORSE: "openai.gpt-oss-120b-1:0",
        ModelRole.JUDGE: "anthropic.claude-opus-5",
        ModelRole.ECONOMY: "us.amazon.nova-micro-v1:0",
    },
    Track.GCP: {
        ModelRole.WORKHORSE: "openai/gpt-oss-120b-maas",
        ModelRole.JUDGE: "claude-opus-5",
        # gpt-oss-20b-maas is deprecated on Google (retirement 2026-10-21, fetched 2026-09-29),
        # so Economy maps to gpt-oss-120b with the Economy token caps until a cheaper open
        # model is generally available.
        ModelRole.ECONOMY: "openai/gpt-oss-120b-maas",
    },
    Track.LOCAL: {
        # gpt-oss:120b is 65 GB and needs the compose gpu profile; a 16 GB laptop runs
        # gpt-oss:20b in both roles. Set NW_MODEL_WORKHORSE=gpt-oss:120b when it fits.
        ModelRole.WORKHORSE: "gpt-oss:20b",
        ModelRole.JUDGE: "fake-judge",
        ModelRole.ECONOMY: "gpt-oss:20b",
    },
}

# The Judge on the Local track when a cloud key is present (NW_ANTHROPIC_API_KEY, or a
# gateway that routes the name to Claude). Without one the role stays on the fake provider
# and the harness runs judge-free.
LOCAL_CLOUD_JUDGE = "claude-opus-5"

# The models every role resolves to under NW_PROVIDER=fake, on any track. Tests and the
# service-layer exercises run here: no network, no account.
FAKE_MODELS: dict[ModelRole, str] = {
    ModelRole.WORKHORSE: "fake-workhorse",
    ModelRole.JUDGE: "fake-judge",
    ModelRole.ECONOMY: "fake-economy",
}


def is_claude(model: str) -> bool:
    """Claude ids on every vendor: `anthropic.claude-*`, `us.anthropic.claude-*`,
    `claude-*@date`, and the gateway's plain `claude-*` names."""
    return "claude" in model.lower()


# Field names that hold credentials. Never printed, never hashed.
SECRET_MARKERS = ("key", "secret", "token", "password")


def is_secret(name: str) -> bool:
    return any(m in name.lower() for m in SECRET_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NW_", env_file=".env", extra="ignore")

    track: Track = Track.LOCAL
    # `fake` answers every role from the fake provider, on any track; `auto` routes by
    # track and model id (`nw.llm.providers.make_provider`).
    provider: ProviderMode = ProviderMode.AUTO

    # The tenant this process belongs to on a cohort platform and the environment it runs
    # in. Both travel on logs and cost records; the gateway key already carries the tenant.
    tenant: str | None = None
    environment: str | None = None

    # The model gateway (LiteLLM on the platform). When set, every role goes through it:
    # the role's model id is the gateway's model name and `gateway_key` is the tenant's
    # virtual key, sent as a bearer token.
    gateway_url: str | None = None
    gateway_key: str | None = None

    aws_region: str = "us-east-1"
    aws_profile: str | None = None

    gcp_project: str | None = None
    gcp_region: str = "global"
    # The platform region for Cloud Run, Pipelines, RAG Engine and Agent Engine; the model
    # region above stays global.
    gcp_platform_region: str = Field(
        "us-central1", validation_alias=AliasChoices("NW_GCP_PLATFORM_REGION", "NW_GCP_RUN_REGION")
    )
    # The region of the OpenAI-compatible managed API for open models (gpt-oss). Claude
    # keeps `gcp_region`; gpt-oss-20b is served only from us-central1.
    gcp_maas_region: str = "us-central1"

    # Local track: Ollama's OpenAI-compatible base URL, and the key that turns the Judge
    # role into a real Claude call through the Anthropic API.
    ollama_url: str = "http://localhost:11434/v1"
    anthropic_api_key: str | None = None

    model_workhorse: str | None = None
    model_judge: str | None = None
    model_economy: str | None = None

    # A second model per role, tried once when the primary fails with a terminal 404 or a
    # 400 that names the model, or exhausts its retries. Unset means no fallback.
    model_fallback_workhorse: str | None = None
    model_fallback_judge: str | None = None
    model_fallback_economy: str | None = None

    max_concurrency: int = Field(default=8, ge=1)
    spend_cap_usd: float = Field(default=10.0, ge=0)
    request_timeout_s: float = Field(default=60.0, gt=0)
    # The circuit breaker per model: open after this many consecutive failures, for this long.
    breaker_failures: int = Field(default=3, ge=1)
    breaker_open_s: float = Field(default=30.0, gt=0)
    log_format: str = "json"

    # Read here only so `describe()` can say whether it is set. `nw.auth` reads the key.
    api_key: str | None = None

    def model_for(self, role: ModelRole) -> str:
        override = {
            ModelRole.WORKHORSE: self.model_workhorse,
            ModelRole.JUDGE: self.model_judge,
            ModelRole.ECONOMY: self.model_economy,
        }[role]
        if override:
            return override
        if self.provider is ProviderMode.FAKE:
            return FAKE_MODELS[role]
        if role is ModelRole.JUDGE and self.track is Track.LOCAL and self.has_cloud_key:
            return LOCAL_CLOUD_JUDGE
        return DEFAULT_MODELS[self.track][role]

    @property
    def has_cloud_key(self) -> bool:
        """Whether a Local-track process can reach a cloud Judge: a gateway or an
        Anthropic API key."""
        return bool(self.gateway_url or self.anthropic_api_key)

    @property
    def uses_gateway(self) -> bool:
        return bool(self.gateway_url) and self.provider is not ProviderMode.FAKE

    def fallback_for(self, role: ModelRole) -> str | None:
        """The fallback model for a role, or None. A fallback equal to the primary is no
        fallback at all and is reported as None."""
        fallback = {
            ModelRole.WORKHORSE: self.model_fallback_workhorse,
            ModelRole.JUDGE: self.model_fallback_judge,
            ModelRole.ECONOMY: self.model_fallback_economy,
        }[role]
        return fallback if fallback and fallback != self.model_for(role) else None

    @property
    def fallback(self) -> dict[ModelRole, str]:
        """Every role with a fallback configured, as a map."""
        out = {}
        for role in ModelRole:
            fb = self.fallback_for(role)
            if fb:
                out[role] = fb
        return out

    # ----- governance ------------------------------------------------------------------

    def describe(
        self,
        *,
        env: Mapping[str, str] | None = None,
        env_file: str | Path | None = ".env",
    ) -> list[dict[str, Any]]:
        """Every setting with its effective value and where it came from: `env`, `.env`,
        `init` (a value passed to the constructor) or `default`. Secret values are redacted
        to `set` or `unset` and never returned."""
        environ = dict(os.environ) if env is None else dict(env)
        dotenv = _read_dotenv(env_file)
        prefix = self.model_config.get("env_prefix", "") or ""
        rows = []
        for name, info in type(self).model_fields.items():
            var = f"{prefix}{name}".upper()
            value = getattr(self, name)
            default = info.get_default(call_default_factory=True)
            if var in environ:
                source = "env"
            elif var in dotenv:
                source = ".env"
            elif value != default:
                source = "init"
            else:
                source = "default"
            secret = is_secret(name)
            shown: Any = ("set" if value else "unset") if secret else _plain(value)
            rows.append(
                {"name": name, "env": var, "value": shown, "source": source, "secret": secret}
            )
        return rows

    def config_hash(self) -> str:
        """Twelve hex characters over every non-secret setting's effective value."""
        payload = {
            name: _plain(getattr(self, name))
            for name in type(self).model_fields
            if not is_secret(name)
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, StrEnum) else value


def _read_dotenv(env_file: str | Path | None) -> dict[str, str]:
    if not env_file or not Path(env_file).exists():
        return {}
    from dotenv import dotenv_values

    return {k.upper(): v or "" for k, v in dotenv_values(env_file).items()}


def format_describe(rows: list[dict[str, Any]], config_hash: str) -> str:
    width = max(len(r["env"]) for r in rows)
    lines = [f"{'setting':<{width}}  {'source':<8}  value"]
    for r in rows:
        value = "<redacted, " + str(r["value"]) + ">" if r["secret"] else json.dumps(r["value"])
        lines.append(f"{r['env']:<{width}}  {r['source']:<8}  {value}")
    lines.append(f"config_hash {config_hash}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


if __name__ == "__main__":
    s = settings()
    print(format_describe(s.describe(), s.config_hash()))
