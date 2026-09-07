"""Settings for every service in the course.

One object, read once at startup. Code asks for a model by role, never by ID,
and never imports a cloud SDK directly: that is the provider's job.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

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

    max_concurrency: int = Field(default=8, ge=1)
    spend_cap_usd: float = Field(default=10.0, ge=0)
    request_timeout_s: float = Field(default=60.0, gt=0)
    log_format: str = "json"

    def model_for(self, role: ModelRole) -> str:
        override = {
            ModelRole.WORKHORSE: self.model_workhorse,
            ModelRole.JUDGE: self.model_judge,
            ModelRole.ECONOMY: self.model_economy,
        }[role]
        return override or DEFAULT_MODELS[self.track][role]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
