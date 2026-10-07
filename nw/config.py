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

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Track(StrEnum):
    AWS = "aws"
    GCP = "gcp"
    LOCAL = "local"
    AZURE = "azure"


class ProviderMode(StrEnum):
    """`auto` picks the track's real providers; `fake` scripts every role in memory."""

    AUTO = "auto"
    FAKE = "fake"


class ModelRole(StrEnum):
    WORKHORSE = "workhorse"
    JUDGE = "judge"
    ECONOMY = "economy"


class Residency(StrEnum):
    """Where a call's data may be processed. `default` is the track's own region; `eu` keeps
    prompts and completions inside the EU (an account with `region: eu` in
    `data/accounts.json`, the promise in `data/policies/gdpr-data-residency.md`)."""

    DEFAULT = "default"
    EU = "eu"


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
# - Microsoft Foundry (learn.microsoft.com/azure/foundry/foundry-models/concepts/
#   models-sold-directly-by-azure, models-from-partners and openai/concepts/
#   model-retirement-schedule, all fetched 2026-09-29, pages dated 2026-09-21): a role's id is
#   the Foundry deployment name, which `deploy/azure` sets equal to the model id.
#   `gpt-oss-120b` (format OpenAI-OSS, version 1, GlobalStandard, needs a Foundry project; the
#   capability table marks it Preview, the retirement schedule GA with no retirement date) on
#   the v1 chat completions endpoint `https://<resource>.services.ai.azure.com/openai/v1`.
#   `gpt-oss-20b` is served only on managed compute and Foundry Local, not as a serverless
#   deployment. Economy is `mistral-small-2503` (Mistral Small 3.1, format Mistral AI,
#   version 1, GA with no retirement date, Global Standard in eastus2, tool calling and JSON
#   output, sold through Azure Marketplace): 0.10 and 0.30 USD per million tokens against the
#   Workhorse's 0.15 and 0.60, so the cheap-first router saves on both sides. Rejected:
#   gpt-5.4-nano (0.20 and 1.25, dearer than the Workhorse), gpt-5-nano (0.05 and 0.40, retires
#   2027-02-09), gpt-4.1-nano (retires 2026-10-14), Phi-4-mini-instruct (no tool calling),
#   Ministral-3B (cheapest, but 3B parameters for a multi-step tool loop). The model is not a
#   reasoning model, so it takes a temperature. Claude is on the Messages API at
#   `https://<resource>.services.ai.azure.com/anthropic`: Foundry offers claude-opus-5-5,
#   claude-opus-5, claude-opus-4-8, 4-7, 4-6, 4-5, claude-sonnet-5-5, claude-sonnet-5,
#   4-6, 4-5, claude-haiku-4-5, claude-fable-5 and 5-1 (preview) and the gated Mythos models.
#   The Judge is `claude-opus-5` (GA, retires 2027-07-08), the model the judge was calibrated
#   against on the other clouds. All three deploy GlobalStandard in eastus2.
# - Ollama (ollama.com/library/gpt-oss): tags `gpt-oss:20b` (14 GB) and `gpt-oss:120b`
#   (65 GB), both with the `tools` and `thinking` capabilities.
DEFAULT_MODELS: dict[Track, dict[ModelRole, str]] = {
    Track.AWS: {
        ModelRole.WORKHORSE: "openai.gpt-oss-120b-1:0",
        # Opus 5 has no in-region on-demand row in us-east-1 (model card, 2026-09-30): the US geo
        # profile serves it, the same one deploy/aws grants.
        # Opus 5 is restricted on new accounts; Opus 4.5 is generally available.
        ModelRole.JUDGE: "us.anthropic.claude-opus-4-5-20251101-v1:0",
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
    Track.AZURE: {
        ModelRole.WORKHORSE: "gpt-oss-120b",
        ModelRole.JUDGE: "claude-opus-5",
        ModelRole.ECONOMY: "mistral-small-2503",
    },
    Track.LOCAL: {
        # gpt-oss:120b is 65 GB and needs the compose gpu profile; a 16 GB laptop runs
        # gpt-oss:20b in both roles. Set NW_MODEL_WORKHORSE=gpt-oss:120b when it fits.
        ModelRole.WORKHORSE: "gpt-oss:20b",
        ModelRole.JUDGE: "fake-judge",
        ModelRole.ECONOMY: "gpt-oss:20b",
    },
}

# EU residency (06 H5): the model id per role that keeps processing in the EU, per track,
# fetched 2026-09-30. None means the track has no EU-resident model for the role today; the
# client then refuses the call (`ResidencyError`) instead of sending EU data elsewhere, and
# `NW_MODEL_EU_<ROLE>` names one when the operator has deployed it.
# - Bedrock, from `aws_eu_region` (eu-central-1, Frankfurt), model cards at
#   docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html: gpt-oss-120b in-region in
#   eu-central-1 (no geo profile exists); Claude Opus 5 through the EU geo profile
#   `eu.anthropic.claude-opus-5` on bedrock-runtime ("keeps data within EU regions"); Nova
#   Micro through `eu.amazon.nova-micro-v1:0` (Frankfurt routes to Frankfurt, Stockholm,
#   Ireland, Paris).
# - Google: Claude Opus 5 on the `eu` multi-region endpoint (aiplatform.eu.rep.googleapis.com,
#   platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai and the Opus 5 model page,
#   "ML processing: Europe Multi-region"). gpt-oss-120b-maas and gpt-oss-20b-maas process in the
#   US multi-region only (model pages, updated 2026-09-28), so Workhorse and Economy have no EU
#   managed id: deploy gpt-oss from Model Garden to an EU endpoint and set NW_MODEL_EU_*.
# - Microsoft Foundry, Data Zone Standard in the EU data zone (learn.microsoft.com/azure/foundry/
#   foundry-models/concepts/models-sold-directly-by-azure-region-availability and
#   models-from-partners, fetched 2026-09-30): neither gpt-oss-120b, mistral-small-2503 nor any
#   Claude model has an EU data zone deployment (Claude's Data Zone Standard is US only).
#   Mistral-Large-3 does (francecentral, germanywestcentral, swedencentral and others), so it
#   serves Workhorse and Economy on the EU resource (`NW_AZURE_FOUNDRY_EU_ENDPOINT`), deployed
#   with SKU `DataZoneStandard` under its own name. The Judge has no EU model.
# - Local: nothing leaves the laptop, so the Ollama roles serve EU accounts as they are; the
#   Judge stays on the fake provider for EU calls even when a cloud key is set.
EU_MODELS: dict[Track, dict[ModelRole, str | None]] = {
    Track.AWS: {
        ModelRole.WORKHORSE: "openai.gpt-oss-120b-1:0",
        ModelRole.JUDGE: "eu.anthropic.claude-opus-4-5-20251101-v1:0",
        ModelRole.ECONOMY: "eu.amazon.nova-micro-v1:0",
    },
    Track.GCP: {
        ModelRole.WORKHORSE: None,
        ModelRole.JUDGE: "claude-opus-5",
        ModelRole.ECONOMY: None,
    },
    Track.AZURE: {
        ModelRole.WORKHORSE: "Mistral-Large-3",
        ModelRole.JUDGE: None,
        ModelRole.ECONOMY: "Mistral-Large-3",
    },
    Track.LOCAL: {
        ModelRole.WORKHORSE: "gpt-oss:20b",
        ModelRole.JUDGE: "fake-judge",
        ModelRole.ECONOMY: "gpt-oss:20b",
    },
}

# Through a LiteLLM gateway the EU deployments are separate gateway models, named with this
# prefix before the EU id (`eu/openai.gpt-oss-120b-1:0`), so one gateway serves both zones.
GATEWAY_EU_PREFIX = "eu/"

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
FAKE_EU_MODELS: dict[ModelRole, str] = {role: f"{m}-eu" for role, m in FAKE_MODELS.items()}

# Hugging Face models the course loads by name, pinned to a commit (06 M15): a name alone
# follows `main`, so a push to the model repo would change what an image serves. Commit ids
# fetched from the Hub API on 2026-09-30 (`/api/models/<id>/revision/main`). Pass
# `revision=hf_revision(name)` to every `from_pretrained`, `SentenceTransformer` and
# `CrossEncoder` that loads by name; a local directory needs no revision.
HF_REVISIONS: dict[str, str] = {
    "sentence-transformers/all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "cross-encoder/ms-marco-MiniLM-L6-v2": "233902d25c440f23af6f7d6e94d2946bac0bee0a",
    "distilbert/distilbert-base-uncased": "12040accade4e8a0f71eabdb258fecc2e7e948be",
}
# Old names the Hub redirects to the ids above.
HF_ALIASES: dict[str, str] = {
    "cross-encoder/ms-marco-MiniLM-L-6-v2": "cross-encoder/ms-marco-MiniLM-L6-v2",
    "distilbert-base-uncased": "distilbert/distilbert-base-uncased",
}


def hf_revision(name: str) -> str | None:
    """The pinned commit for a Hub model id, None for a local path. An unpinned Hub id raises:
    add it to `HF_REVISIONS` with the commit you tested, or pin it with
    `NW_HF_REVISION_<ID>` (non-alphanumerics as `_`, upper case)."""
    if not name or Path(name).exists() or name.startswith((".", "/", "~")):
        return None
    env = "NW_HF_REVISION_" + "".join(c if c.isalnum() else "_" for c in name).upper()
    if os.environ.get(env):
        return os.environ[env]
    canonical = HF_ALIASES.get(name, name)
    if canonical in HF_REVISIONS:
        return HF_REVISIONS[canonical]
    raise ValueError(
        f"no pinned revision for Hugging Face model {name!r}: add it to nw.config.HF_REVISIONS "
        f"or set {env}"
    )


def is_claude(model: str) -> bool:
    """Claude ids on every vendor: `anthropic.claude-*`, `us.anthropic.claude-*`,
    `claude-*@date`, and the gateway's plain `claude-*` names."""
    return "claude" in model.lower()


# Field names that hold credentials. Never printed, never hashed. A connection string carries
# an instrumentation key (App Insights) or a password (a database), so it counts as one.
SECRET_MARKERS = ("key", "secret", "token", "password", "connection_string")


# Names that match a marker but hold no credential: a vault's name is not a secret.
NOT_SECRET = frozenset({"azure_key_vault"})


# `make deploy-azure` writes the deployment outputs here (`scripts/deploy_azure.sh`), keyed by
# the same `NW_AZURE_*` names as the settings. `NW_AZURE_OUTPUTS` points elsewhere; empty
# turns the file off (the test suite does, so a deployed checkout cannot change a test).
AZURE_OUTPUTS = Path(__file__).resolve().parents[1] / "deploy" / "azure" / "outputs.json"
# Settings filled from the outputs file when the environment and `.env` leave them unset.
AZURE_FROM_OUTPUTS = ("azure_apim_gateway_url",)


def azure_outputs_path(env: Mapping[str, str] | None = None) -> Path | None:
    e = os.environ if env is None else env
    if "NW_AZURE_OUTPUTS" in e:
        return Path(e["NW_AZURE_OUTPUTS"]) if e["NW_AZURE_OUTPUTS"] else None
    return AZURE_OUTPUTS


def read_azure_outputs(path: Path | None) -> dict[str, str]:
    """`deploy/azure/outputs.json`: `{"NW_AZURE_ACR": "..."}`, or the ARM deployment outputs
    `{"NW_AZURE_ACR": {"type": "String", "value": "..."}}`. Missing file, empty dict."""
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "properties" in data:  # `az deployment ... show` whole
        data = data["properties"].get("outputs", {})
    out: dict[str, str] = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        if isinstance(value, str | int | float):
            out[key.upper() if key.upper().startswith("NW_") else key] = str(value)
    return out


def is_secret(name: str) -> bool:
    if name.lower() in NOT_SECRET:
        return False
    return any(m in name.lower() for m in SECRET_MARKERS)


# The dotenv file Settings reads: `.env` in the working directory, another path with
# `NW_ENV_FILE`, none when it is empty. The test suite sets it empty, so a learner's own `.env`
# (a gateway key, a lighter model) never changes what the tests see.
ENV_FILE: str | None = os.environ.get("NW_ENV_FILE", ".env") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NW_", env_file=ENV_FILE, extra="ignore")

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
    # The region EU-resident calls go to (every EU role is served from Frankfurt).
    aws_eu_region: str = "eu-central-1"

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
    # The Claude endpoint for EU-resident calls: the `eu` multi-region.
    gcp_eu_region: str = "eu"

    # Azure track (ADR 0013). Each has an env override and `deploy/azure/outputs.json` fills
    # the rest (`nw.platform.azure.AzureConfig`). The Foundry endpoint is the resource's
    # `https://<resource>.services.ai.azure.com`; the APIM gateway, when set, is the model
    # gateway and `gateway_key` is the tenant's APIM subscription key.
    azure_subscription_id: str | None = None
    azure_resource_group: str | None = None
    azure_location: str = "eastus2"
    azure_ml_workspace: str | None = None
    azure_foundry_endpoint: str | None = None
    azure_foundry_project: str | None = None
    # An API key for the Foundry resource (from Key Vault, injected by reference). Unset means
    # Microsoft Entra ID through DefaultAzureCredential, which is the default.
    azure_foundry_key: str | None = None
    # A second Foundry resource in an EU region holding the Data Zone Standard (EU) deployments
    # that EU-resident calls use. Unset means EU calls on the azure track are refused.
    azure_foundry_eu_endpoint: str | None = None
    azure_search_endpoint: str | None = None
    azure_key_vault: str | None = None
    azure_acr: str | None = None
    azure_storage_account: str | None = None
    azure_appinsights_connection_string: str | None = None
    # Unset here and in `.env`, it is read from `deploy/azure/outputs.json` on the azure track,
    # so a learner never copies it. Empty (`NW_AZURE_APIM_GATEWAY_URL=`) means no APIM.
    azure_apim_gateway_url: str | None = None
    azure_containerapps_env: str | None = None

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

    # The EU-resident model per role, overriding `EU_MODELS` (an operator's own EU deployment).
    # EU calls never fall back to a model outside the zone, so there is no EU fallback.
    model_eu_workhorse: str | None = None
    model_eu_judge: str | None = None
    model_eu_economy: str | None = None
    # Off turns residency routing off: every call uses the default ids whatever the account.
    residency_routing: bool = True

    max_concurrency: int = Field(default=8, ge=1)
    spend_cap_usd: float = Field(default=10.0, ge=0)
    request_timeout_s: float = Field(default=60.0, gt=0)
    # The whole call, every retry, back-off and the fallback included. Without it one call can
    # take attempts x timeout plus the back-offs, twice over with a fallback (about 11 minutes).
    call_deadline_s: float = Field(default=180.0, gt=0)
    # The circuit breaker per model: open after this many consecutive failures, for this long.
    breaker_failures: int = Field(default=3, ge=1)
    breaker_open_s: float = Field(default=30.0, gt=0)
    log_format: str = "json"

    # Read here only so `describe()` can say whether it is set. `nw.auth` reads the key.
    api_key: str | None = None
    # `NW_AUTH_DISABLED=1` lets a service start without a key off the local track; read by
    # `nw.auth` from the environment, shown here so `describe()` reports it.
    auth_disabled: bool = False

    _from_outputs: set[str] = PrivateAttr(default_factory=set)

    @model_validator(mode="after")
    def _azure_outputs(self) -> Settings:
        """On the azure track, fill what the deployment knows and nobody set."""
        if self.track is not Track.AZURE:
            return self
        missing = [n for n in AZURE_FROM_OUTPUTS if getattr(self, n) is None]
        if not missing:
            return self
        out = read_azure_outputs(azure_outputs_path())
        for name in missing:
            value = out.get(f"NW_{name.upper()}")
            if value:
                object.__setattr__(self, name, value)
                self._from_outputs.add(name)
        return self

    def model_for(self, role: ModelRole, residency: Residency = Residency.DEFAULT) -> str:
        if residency is Residency.EU and self.residency_routing:
            return self.eu_model_for(role)
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

    def eu_model_for(self, role: ModelRole) -> str:
        """The EU-resident id for a role: the `NW_MODEL_EU_<ROLE>` override, the fake EU id under
        `NW_PROVIDER=fake`, else `EU_MODELS` for the track, prefixed for a LiteLLM gateway.
        Raises `ResidencyError` when the track has none: an EU call is refused, never sent
        to a model outside the zone."""
        override = {
            ModelRole.WORKHORSE: self.model_eu_workhorse,
            ModelRole.JUDGE: self.model_eu_judge,
            ModelRole.ECONOMY: self.model_eu_economy,
        }[role]
        if override:
            return override
        if self.provider is ProviderMode.FAKE:
            return FAKE_EU_MODELS[role]
        model = EU_MODELS[self.track][role]
        if model is None:
            from nw.llm.errors import ResidencyError

            raise ResidencyError(
                f"no EU-resident model for the {role.value} role on the {self.track.value} "
                f"track; set NW_MODEL_EU_{role.name} to an EU deployment"
            )
        if self.gateway_url and self.track is not Track.LOCAL:
            return GATEWAY_EU_PREFIX + model
        return model

    def judge_unavailable(self) -> str | None:
        """Why a judged run cannot score here, or None. On the Local track without a Claude
        route the Judge resolves to `fake-judge`; outside `NW_PROVIDER=fake` that would score
        real answers with canned verdicts, so a judged harness stops before any model call."""
        if self.provider is ProviderMode.FAKE:
            return None
        if self.model_for(ModelRole.JUDGE).startswith("fake-"):
            return (
                "no Judge route on this track: set ANTHROPIC_API_KEY in .env and restart the "
                "stack (or NW_ANTHROPIC_API_KEY), set NW_MODEL_JUDGE, or run without the Judge "
                "(--no-judge, NO_JUDGE=1)"
            )
        return None

    @property
    def has_cloud_key(self) -> bool:
        """Whether a Local-track process can reach a cloud Judge: its own Anthropic API key, or
        a gateway that serves Claude. The Local gateway serves Claude only when it was started
        with `ANTHROPIC_API_KEY` (compose reads it from the same `.env`); a gateway without it
        answers `claude-opus-5` with 400, so the Judge stays on the fake provider."""
        if self.anthropic_api_key:
            return True
        if not self.gateway_url:
            return False
        if self.track is not Track.LOCAL:
            return True
        return bool(
            os.environ.get("ANTHROPIC_API_KEY") or _read_dotenv(ENV_FILE).get("ANTHROPIC_API_KEY")
        )

    @property
    def uses_apim(self) -> bool:
        """API Management's AI gateway in front of Foundry: the azure track with its URL set and
        no LiteLLM gateway, which wins when both are set (`make_provider`)."""
        return (
            self.track is Track.AZURE
            and bool(self.azure_apim_gateway_url)
            and not self.gateway_url
            and self.provider is not ProviderMode.FAKE
        )

    @property
    def uses_gateway(self) -> bool:
        """A model gateway is in the path: LiteLLM on any track, or API Management on Azure."""
        if self.provider is ProviderMode.FAKE:
            return False
        return bool(self.gateway_url) or self.uses_apim

    def fallback_for(self, role: ModelRole, residency: Residency = Residency.DEFAULT) -> str | None:
        """The fallback model for a role, or None. A fallback equal to the primary is no
        fallback at all and is reported as None. EU calls have none: the configured fallbacks
        are not known to be EU-resident."""
        if residency is Residency.EU and self.residency_routing:
            return None
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
        env_file: str | Path | None = ENV_FILE,
    ) -> list[dict[str, Any]]:
        """Every setting with its effective value and where it came from: `env`, `.env`,
        `outputs` (`deploy/azure/outputs.json`), `init` (a value passed to the constructor) or
        `default`. Secret values are redacted
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
            elif name in self._from_outputs:
                source = "outputs"
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
