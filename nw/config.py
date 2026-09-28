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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Track(StrEnum):
    AWS = "aws"
    GCP = "gcp"
    LOCAL = "local"


class ModelRole(StrEnum):
    WORKHORSE = "workhorse"
    JUDGE = "judge"
    ECONOMY = "economy"


# Track defaults. Verify these IDs against the provider's model list before each
# delivery: `make preflight` does one round trip per role.
DEFAULT_MODELS: dict[Track, dict[ModelRole, str]] = {
    Track.AWS: {
        ModelRole.WORKHORSE: "anthropic.claude-sonnet-5",
        ModelRole.JUDGE: "anthropic.claude-opus-5",
        ModelRole.ECONOMY: "anthropic.claude-haiku-4-5",
    },
    Track.GCP: {
        ModelRole.WORKHORSE: "claude-sonnet-5",
        ModelRole.JUDGE: "claude-opus-5",
        ModelRole.ECONOMY: "claude-haiku-4-5@20251001",
    },
    Track.LOCAL: {
        ModelRole.WORKHORSE: "fake-workhorse",
        ModelRole.JUDGE: "fake-judge",
        ModelRole.ECONOMY: "fake-economy",
    },
}

# Field names that hold credentials. Never printed, never hashed.
SECRET_MARKERS = ("key", "secret", "token", "password")


def is_secret(name: str) -> bool:
    return any(m in name.lower() for m in SECRET_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NW_", env_file=".env", extra="ignore")

    track: Track = Track.LOCAL

    aws_region: str = "us-east-1"
    aws_profile: str | None = None

    gcp_project: str | None = None
    gcp_region: str = "global"

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
        return override or DEFAULT_MODELS[self.track][role]

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
