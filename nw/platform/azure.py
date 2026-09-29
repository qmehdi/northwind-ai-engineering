"""The Azure track of the platform contract (ADR 0013): Azure Machine Learning for the model
registry, pipelines and managed online endpoints; Microsoft Foundry for prompts' models, the
agent runtime and the guardrail; Azure AI Search for retrieval; Blob Storage for the documents
no service has a resource for. Six protocols of `nw.platform.base`, one `build(settings)`.

Every SDK is imported lazily through `AzureClients`, so the module imports on any track and
tests hand in fakes. Install the SDKs with `uv sync --extra platform-azure`.

    uv run python -m nw.platform.azure describe         # what build(settings) resolved, the names
    uv run python -m nw.platform.azure pipeline-definition triage   # the Azure ML job as YAML

Configuration comes from the environment (`NW_AZURE_*`) and, after `make deploy-azure`, from
`deploy/azure/outputs.json`, a flat JSON object keyed by the same `NW_AZURE_*` names (the ARM
form `{"name": {"type": ..., "value": ...}}` is read too). The environment wins.

Naming (`names()`): a tenant's resources are `northwind-alice-<kind>` wherever Azure allows
hyphens (the Azure ML model and experiment, the AI Search index, the hosted agent, blob
prefixes). Storage accounts, key vaults and registries take lowercase letters and digits only
and are globally unique, so they are `nw<env><kind>` plus a suffix from the deployment, cut to
the service's limit (`compact_name`); the code never derives them, it reads them from the
outputs. Managed online endpoint names are unique per region across all customers and at most
32 characters, so they are `nw-<owner>-<kind>-<h5>`, where `<h5>` is five hex characters of the
SHA-256 of subscription, resource group and environment (`endpoint_name`).

What the platform has no resource or API for lives in the artifacts container of the storage
account, as on Google Cloud:
- the stage audit trail of model versions (`<prefix>/registry/<name>/stages.jsonl`)
- prompt texts and their stage index (the Azure ML data asset per version points at the text)
- the agent registry document `agents/agents.json`
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nw.config import Settings, Track, read_azure_outputs
from nw.platform.base import (
    Hit,
    ModelVersion,
    PipelineRun,
    Platform,
    PromptVersion,
    RunStatus,
    Stage,
    Tenant,
)

OUTPUTS = Path(__file__).resolve().parents[2] / "deploy" / "azure" / "outputs.json"
LIVE = "live"
SCOPE = "https://ai.azure.com/.default"
STAGE_TAG = "stage"
# Which stage wins when several versions claim one; the previous holder steps back.
STAGE_PRIORITY: tuple[Stage, ...] = (Stage.LIVE, Stage.APPROVED, Stage.RETIRED, Stage.CANDIDATE)
STEP_BACK = {Stage.LIVE: Stage.RETIRED, Stage.APPROVED: Stage.CANDIDATE}
# Azure ML job states (azure.ai.ml JobStatus) onto the course's run states.
JOB_STATES: Mapping[str, RunStatus] = {
    "NotStarted": RunStatus.QUEUED,
    "Queued": RunStatus.QUEUED,
    "Preparing": RunStatus.QUEUED,
    "Provisioning": RunStatus.QUEUED,
    "Starting": RunStatus.QUEUED,
    "Running": RunStatus.RUNNING,
    "Finalizing": RunStatus.RUNNING,
    "NotResponding": RunStatus.RUNNING,
    "Paused": RunStatus.RUNNING,
    "CancelRequested": RunStatus.STOPPED,
    "Canceled": RunStatus.STOPPED,
    "Completed": RunStatus.SUCCEEDED,
    "Failed": RunStatus.FAILED,
}
# The serving images answer the Agent Platform custom container contract (nw/serving/vertex.py)
# when AIP_HTTP_PORT is set; Azure ML's custom container contract is the same three routes, so
# one image serves Vertex, Azure ML and a tenant's Container App alike.
SERVING_PORT = 8080
INFERENCE_CONFIG: Mapping[str, Any] = {
    "liveness_route": {"port": SERVING_PORT, "path": "/health"},
    "readiness_route": {"port": SERVING_PORT, "path": "/health"},
    "scoring_route": {"port": SERVING_PORT, "path": "/predict"},
}
# Azure ML mounts the registered model at `<model_mount_path>/<model name>/<version>`
# (how-to-deploy-custom-container, fetched 2026-09-29); nw/serving/download.py copies the tree and
# finds the directory holding metadata.json, so the service needs only the mount root.
MODEL_MOUNT = "/var/nw/model"
DEPLOYMENT_COLORS = ("blue", "green")
# The agent container: the Foundry hosted agent gateway calls port 8088 (deploy-hosted-agent,
# fetched 2026-09-29) with the Invocations protocol, `/invocations` and `/readiness`.
AGENT_PORT = 8088
AGENT_PROTOCOL_VERSION = "2.0.0"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ----- naming -----------------------------------------------------------------------------


# Limits from "Naming rules and restrictions for Azure resources" (learn.microsoft.com/azure/
# azure-resource-manager/management/resource-name-rules) and the service pages, 2026-09-29.
LIMITS: Mapping[str, int] = {
    "storage": 24,  # 3 to 24, lowercase letters and digits, globally unique
    "keyvault": 24,  # 3 to 24, letters, digits and hyphens; the compact form has none
    "acr": 50,  # 5 to 50, letters and digits, globally unique
    "endpoint": 32,  # managed online endpoint: 3 to 32, unique within the region
    "deployment": 32,
    "agent": 63,  # hosted agent: alphanumeric and hyphens, 63
    "search_index": 128,  # lowercase letters, digits and dashes
    "containerapp": 32,  # 2 to 32, lowercase letters, digits and hyphens
}


def compact_name(*parts: str, limit: int, suffix: str = "", prefix: str = "nw") -> str:
    """`nw<parts><suffix>` in lowercase letters and digits, at most `limit` characters: for
    storage accounts (24), key vaults (24) and container registries (50). When the parts do
    not fit they are cut and four hex characters of their SHA-256 keep two long names apart.
    `suffix` is the deployment's uniqueness token (Bicep `uniqueString(resourceGroup().id)`)
    and is never cut."""
    body = re.sub(r"[^a-z0-9]", "", "".join(parts).lower())
    fixed = prefix + suffix
    room = limit - len(fixed)
    if room < 1:
        raise ValueError(f"no room for a name in {limit} characters after {fixed!r}")
    if len(body) > room:
        digest = hashlib.sha256(body.encode()).hexdigest()[:4]
        body = body[: max(room - 4, 0)] + digest
        body = body[:room]
    name = prefix + body + suffix
    if len(name) < 3:
        raise ValueError(f"{name!r} is shorter than the 3 characters Azure requires")
    return name


def scope_hash(subscription_id: str, resource_group: str, environment: str, n: int = 5) -> str:
    """Hex characters that make a region-unique name unique to this deployment."""
    key = f"{subscription_id}/{resource_group}/{environment}".lower()
    return hashlib.sha256(key.encode()).hexdigest()[:n]


def endpoint_name(owner: str, kind: str, scope: str) -> str:
    """`nw-alice-triage-1a2b3`: a managed online endpoint name, 32 characters at most, starting
    with a letter, letters, digits and hyphens. `owner` is the tenant's handle or `live`; the
    owner is cut first when the name is too long, the kind and the scope hash never are."""
    owner = re.sub(r"[^a-z0-9]", "", owner.lower())
    kind = re.sub(r"[^a-z0-9-]", "", kind.lower())
    fixed = f"nw--{kind}-{scope}"
    room = LIMITS["endpoint"] - len(fixed)
    return f"nw-{owner[: max(room, 1)]}-{kind}-{scope}"


def search_index_name(tenant: Tenant, collection: str) -> str:
    """`northwind-alice-policies`: lowercase, digits and single dashes, 128 at most."""
    name = re.sub(r"[^a-z0-9-]", "-", tenant.resource(collection).lower())
    return re.sub(r"-{2,}", "-", name).strip("-")[: LIMITS["search_index"]]


def asset_name(tenant: Tenant, kind: str) -> str:
    """An Azure ML asset name (model, data): letters, digits, `-`, `_` and `.`, 255 at most."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", tenant.resource(kind))[:255]


def names(tenant: Tenant, scope: str) -> dict[str, str]:
    """Every name the Azure track gives a tenant's resources, for `describe` and the guide."""
    return {
        "registry model (triage)": asset_name(tenant, "triage"),
        "registry model (semantic)": asset_name(tenant, "semantic"),
        "pipeline experiment": tenant.resource("triage"),
        "online endpoint (triage)": endpoint_name(tenant.name, "triage", scope),
        "online endpoint (semantic)": endpoint_name(tenant.name, "semantic", scope),
        "live endpoint (triage)": endpoint_name(LIVE, "triage", scope),
        "search index": search_index_name(tenant, "policies"),
        "prompt asset (policy.answer)": asset_name(tenant, "prompt-policy.answer"),
        "hosted agent": tenant.resource("agent")[: LIMITS["agent"]],
        "blob prefix": f"{tenant.prefix}/",
    }


# ----- configuration ----------------------------------------------------------------------


def read_outputs(path: Path = OUTPUTS) -> dict[str, str]:
    """`deploy/azure/outputs.json`: `{"NW_AZURE_ACR": "..."}`, or the ARM deployment outputs
    `{"NW_AZURE_ACR": {"type": "String", "value": "..."}}`. Missing file, empty dict."""
    return read_azure_outputs(path)


def _pairs(text: str) -> dict[str, str]:
    out = {}
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class AzureConfig:
    """Everything the implementations need, resolved once from settings, the environment and
    the deployment outputs."""

    subscription_id: str
    resource_group: str
    location: str = "eastus2"
    environment: str = "northwind"
    ml_workspace: str = ""
    foundry_endpoint: str = ""
    foundry_project: str = ""
    search_endpoint: str = ""
    key_vault: str = ""
    acr: str = ""
    storage_account: str = ""
    appinsights_connection_string: str = ""
    apim_gateway_url: str = ""
    containerapps_env: str = ""
    gateway_url: str | None = None
    artifacts_container: str = "artifacts"
    datastore: str = "workspaceblobstore"
    embedding_deployment: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    pipeline_identity_client_id: str = ""
    rai_policy: str = ""
    endpoint_sku: str = "Standard_F2s_v2"
    live_endpoint_sku: str = "Standard_DS3_v2"
    # Data collection on online deployments: always on the live endpoint, opt in for tenants
    # (NW_AZURE_TENANT_DATA_COLLECTION=1), sampled at NW_AZURE_DATA_SAMPLING_RATE.
    tenant_data_collection: bool = False
    data_sampling_rate: float = 1.0
    images: dict[str, str] = field(default_factory=dict)  # project -> image, NW_AZURE_IMAGE_*

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        env: Mapping[str, str] | None = None,
        outputs: Path | Mapping[str, str] = OUTPUTS,
    ) -> AzureConfig:
        e = dict(os.environ if env is None else env)
        out = dict(outputs) if isinstance(outputs, Mapping) else read_outputs(outputs)

        def pick(name: str, attr: str | None = None, default: str = "") -> str:
            var = f"NW_AZURE_{name}"
            from_settings = getattr(settings, attr, None) if attr else None
            return str(e.get(var) or from_settings or out.get(var) or default)

        subscription = pick("SUBSCRIPTION_ID", "azure_subscription_id")
        group = pick("RESOURCE_GROUP", "azure_resource_group")
        if not subscription or not group:
            raise ValueError(
                "NW_AZURE_SUBSCRIPTION_ID and NW_AZURE_RESOURCE_GROUP are required on the azure "
                "track (or deploy/azure/outputs.json from make deploy-azure)"
            )
        environment = (
            getattr(settings, "environment", None)
            or e.get("NW_ENVIRONMENT")
            or out.get("NW_ENVIRONMENT")
            or "northwind"
        )
        apim = pick("APIM_GATEWAY_URL", "azure_apim_gateway_url")
        images = {k: v for k, v in _pairs(out.get("NW_AZURE_IMAGES", "")).items()}
        images.update(
            {
                k[len("NW_AZURE_IMAGE_") :].lower(): v
                for k, v in {**out, **e}.items()
                if k.startswith("NW_AZURE_IMAGE_") and v
            }
        )
        return cls(
            subscription_id=subscription,
            resource_group=group,
            location=e.get("NW_AZURE_LOCATION")
            or out.get("NW_AZURE_LOCATION")
            or getattr(settings, "azure_location", None)
            or "eastus2",
            environment=environment,
            ml_workspace=pick("ML_WORKSPACE", "azure_ml_workspace"),
            foundry_endpoint=pick("FOUNDRY_ENDPOINT", "azure_foundry_endpoint"),
            foundry_project=pick("FOUNDRY_PROJECT", "azure_foundry_project"),
            search_endpoint=pick("SEARCH_ENDPOINT", "azure_search_endpoint"),
            key_vault=pick("KEY_VAULT", "azure_key_vault"),
            acr=pick("ACR", "azure_acr"),
            storage_account=pick("STORAGE_ACCOUNT", "azure_storage_account"),
            appinsights_connection_string=pick(
                "APPINSIGHTS_CONNECTION_STRING", "azure_appinsights_connection_string"
            ),
            apim_gateway_url=apim,
            containerapps_env=pick("CONTAINERAPPS_ENV", "azure_containerapps_env"),
            gateway_url=getattr(settings, "gateway_url", None)
            or e.get("NW_GATEWAY_URL")
            or apim
            or None,
            artifacts_container=pick("ARTIFACTS_CONTAINER", None, "artifacts"),
            datastore=pick("DATASTORE", None, "workspaceblobstore"),
            embedding_deployment=pick("EMBEDDING_DEPLOYMENT", None, "text-embedding-3-small"),
            embedding_dimensions=int(pick("EMBEDDING_DIMENSIONS", None, "1536")),
            pipeline_identity_client_id=pick("PIPELINE_IDENTITY_CLIENT_ID"),
            rai_policy=pick("RAI_POLICY"),
            endpoint_sku=pick("ENDPOINT_SKU", None, "Standard_F2s_v2"),
            live_endpoint_sku=pick("LIVE_ENDPOINT_SKU", None, "Standard_DS3_v2"),
            tenant_data_collection=pick("TENANT_DATA_COLLECTION").lower() in ("1", "true", "yes"),
            data_sampling_rate=float(pick("DATA_SAMPLING_RATE", None, "1.0")),
            images=images,
        )

    @property
    def scope(self) -> str:
        return scope_hash(self.subscription_id, self.resource_group, self.environment)

    @property
    def foundry_root(self) -> str:
        from nw.llm.providers.azure_foundry import resource_root

        if not self.foundry_endpoint:
            raise ValueError("NW_AZURE_FOUNDRY_ENDPOINT is not set")
        return resource_root(self.foundry_endpoint)

    @property
    def project_endpoint(self) -> str:
        """`https://<resource>.services.ai.azure.com/api/projects/<project>`."""
        if "/api/projects/" in self.foundry_endpoint:
            return self.foundry_endpoint.rstrip("/")
        if not self.foundry_project:
            raise ValueError("NW_AZURE_FOUNDRY_PROJECT is not set")
        return f"{self.foundry_root}/api/projects/{self.foundry_project}"

    @property
    def blob_url(self) -> str:
        account = self.storage_account
        if not account:
            raise ValueError("NW_AZURE_STORAGE_ACCOUNT is not set")
        return (
            account
            if account.startswith("https://")
            else (f"https://{account}.blob.core.windows.net")
        )

    @property
    def acr_server(self) -> str:
        if not self.acr:
            return ""
        return self.acr if "." in self.acr else f"{self.acr}.azurecr.io"

    def image_for(self, name: str) -> str:
        if name in self.images:
            return self.images[name]
        if not self.acr_server:
            raise ValueError(
                f"no image for {name!r}: set NW_AZURE_IMAGE_{name.upper()} or NW_AZURE_ACR"
            )
        return f"{self.acr_server}/nw-{name}:latest"

    def datastore_uri(self, path: str) -> str:
        return f"azureml://datastores/{self.datastore}/paths/{path.lstrip('/')}"

    def platform_env(self) -> dict[str, str]:
        """The `NW_AZURE_*` settings a job or container needs to rebuild this config."""
        pairs = {
            "NW_AZURE_SUBSCRIPTION_ID": self.subscription_id,
            "NW_AZURE_RESOURCE_GROUP": self.resource_group,
            "NW_AZURE_LOCATION": self.location,
            "NW_AZURE_ML_WORKSPACE": self.ml_workspace,
            "NW_AZURE_STORAGE_ACCOUNT": self.storage_account,
            "NW_AZURE_ARTIFACTS_CONTAINER": self.artifacts_container,
            "NW_ENVIRONMENT": self.environment,
        }
        return {k: v for k, v in pairs.items() if v}


class AzureClients:
    """Lazy handles on the SDKs. Each property imports on first use; tests replace them with
    `set(...)` before anything is called."""

    def __init__(self, cfg: AzureConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def set(self, **fakes: Any) -> AzureClients:
        self._cache.update(fakes)
        return self

    @property
    def credential(self) -> Any:
        def make() -> Any:
            from azure.identity import DefaultAzureCredential

            return DefaultAzureCredential()

        return self._get("credential", make)

    def token(self, scope: str = SCOPE) -> str:
        return self.credential.get_token(scope).token

    @property
    def ml(self) -> Any:
        def make() -> Any:
            from azure.ai.ml import MLClient

            if not self.cfg.ml_workspace:
                raise ValueError("NW_AZURE_ML_WORKSPACE is not set")
            return MLClient(
                self.credential,
                self.cfg.subscription_id,
                self.cfg.resource_group,
                self.cfg.ml_workspace,
            )

        return self._get("ml", make)

    @property
    def blob(self) -> Any:
        def make() -> Any:
            from azure.storage.blob import BlobServiceClient

            return BlobServiceClient(self.cfg.blob_url, credential=self.credential)

        return self._get("blob", make)

    @property
    def search_index(self) -> Any:
        def make() -> Any:
            from azure.search.documents.indexes import SearchIndexClient

            return SearchIndexClient(self.cfg.search_endpoint, self.credential)

        return self._get("search_index", make)

    def search(self, index: str) -> Any:
        def make() -> Any:
            from azure.search.documents import SearchClient

            return SearchClient(self.cfg.search_endpoint, index, self.credential)

        return self._get(f"search:{index}", make)

    @property
    def projects(self) -> Any:
        def make() -> Any:
            from azure.ai.projects import AIProjectClient

            return AIProjectClient(endpoint=self.cfg.project_endpoint, credential=self.credential)

        return self._get("projects", make)

    @property
    def http(self) -> Any:
        def make() -> Any:
            import httpx

            return httpx.Client(timeout=120)

        return self._get("http", make)


# ----- blob documents ---------------------------------------------------------------------


class Documents:
    """JSON documents in the artifacts container: the store for everything the platform has
    no resource for. Thin on purpose so a fake needs a handful of methods."""

    def __init__(self, clients: AzureClients, container: str) -> None:
        self.clients = clients
        self.container = container

    def _blob(self, path: str) -> Any:
        return self.clients.blob.get_blob_client(container=self.container, blob=path)

    def exists(self, path: str) -> bool:
        return bool(self._blob(path).exists())

    def read_text(self, path: str) -> str | None:
        blob = self._blob(path)
        if not blob.exists():
            return None
        return blob.download_blob().readall().decode("utf-8")

    def read(self, path: str, default: Any = None) -> Any:
        text = self.read_text(path)
        return default if text is None else json.loads(text)

    def write_text(self, path: str, text: str, content_type: str = "text/plain") -> str:
        blob = self._blob(path)
        kwargs: dict[str, Any] = {"overwrite": True}
        with contextlib.suppress(ImportError):
            from azure.storage.blob import ContentSettings

            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        blob.upload_blob(text.encode("utf-8"), **kwargs)
        return self.url(path)

    def write(self, path: str, doc: Any) -> str:
        return self.write_text(path, json.dumps(doc, indent=2, sort_keys=True), "application/json")

    def append(self, path: str, row: Mapping[str, Any]) -> None:
        text = self.read_text(path) or ""
        self.write_text(path, text + json.dumps(row, sort_keys=True) + "\n", "application/x-ndjson")

    def url(self, path: str) -> str:
        return f"{self.clients.cfg.blob_url}/{self.container}/{path}"


# ----- ModelRegistry: Azure ML model registry ---------------------------------------------


def _tags(tenant: Tenant, tags: Mapping[str, str], metrics: Mapping[str, float]) -> dict[str, str]:
    out = {k: str(v) for k, v in tags.items()}
    out.update({f"metric.{k}": f"{float(v):.6g}" for k, v in metrics.items()})
    out.update({"tenant": tenant.name, "environment": tenant.environment})
    return out


def _stage_of(tags: Mapping[str, Any] | None) -> Stage:
    value = (tags or {}).get(STAGE_TAG) or Stage.CANDIDATE.value
    try:
        return Stage(value)
    except ValueError:
        return Stage.CANDIDATE


def _version_key(version: str) -> tuple[int, str]:
    return (int(version), "") if str(version).isdigit() else (sys.maxsize, str(version))


class AzureMLRegistry:
    """The workspace's model registry: one model per tenant and project
    (`northwind-alice-triage`), one integer version per registration, uploaded from the
    artifact directory as a custom model. The stage is the mutable `stage` tag (Azure ML model
    properties are write-once, so they carry what never changes: the registration time and the
    metrics as registered). Moving `live` or `approved` onto a version steps the previous
    holder back (`live` to `retired`, `approved` to `candidate`), which is the promotion; every
    change and its reason is appended to `<prefix>/registry/<name>/stages.jsonl`."""

    def __init__(self, cfg: AzureConfig, clients: AzureClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_container)

    def _models(self, tenant: Tenant, name: str) -> list[Any]:
        try:
            found = list(self.clients.ml.models.list(name=asset_name(tenant, name)))
        except Exception as exc:  # noqa: BLE001 - ResourceNotFoundError without importing it
            if type(exc).__name__ == "ResourceNotFoundError":
                return []
            raise
        return sorted(found, key=lambda m: _version_key(str(m.version)))

    def _to_version(self, name: str, model: Any) -> ModelVersion:
        tags = dict(getattr(model, "tags", None) or {})
        metrics = {
            k[len("metric.") :]: float(v) for k, v in tags.items() if k.startswith("metric.")
        }
        return ModelVersion(
            name=name,
            version=str(model.version),
            stage=_stage_of(tags),
            uri=f"azureml:{model.name}:{model.version}",
            metrics=metrics,
            tags={k: v for k, v in tags.items() if not k.startswith("metric.")},
        )

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
    ) -> ModelVersion:
        from azure.ai.ml.constants import AssetTypes
        from azure.ai.ml.entities import Model

        existing = self._models(tenant, name)
        numbers = [int(m.version) for m in existing if str(m.version).isdigit()]
        version = str(max(numbers, default=0) + 1)
        all_tags = _tags(tenant, tags, metrics)
        all_tags[STAGE_TAG] = Stage.CANDIDATE.value
        model = Model(
            name=asset_name(tenant, name),
            version=version,
            path=str(artifact),
            type=AssetTypes.CUSTOM_MODEL,
            description=f"{name} registered for {tenant.prefix} at {_now()}",
            tags=all_tags,
            properties={
                "nw.registered": _now(),
                "nw.metrics": json.dumps(dict(metrics), sort_keys=True),
            },
        )
        created = self.clients.ml.models.create_or_update(model)
        self.docs.append(
            f"{tenant.prefix}/registry/{name}/stages.jsonl",
            {"version": version, "stage": "candidate", "reason": "registered", "at": _now()},
        )
        return self._to_version(name, created)

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        models = self._models(tenant, name)
        target = next((m for m in models if str(m.version) == str(version)), None)
        if target is None:
            raise KeyError(f"{asset_name(tenant, name)} has no version {version}")
        ml = self.clients.ml
        if stage in STEP_BACK:
            for other in models:
                if str(other.version) != str(version) and _stage_of(other.tags) == stage:
                    other.tags = {**(other.tags or {}), STAGE_TAG: STEP_BACK[stage].value}
                    ml.models.create_or_update(other)
                    self.docs.append(
                        f"{tenant.prefix}/registry/{name}/stages.jsonl",
                        {
                            "version": str(other.version),
                            "stage": STEP_BACK[stage].value,
                            "reason": f"replaced by version {version}",
                            "at": _now(),
                        },
                    )
        target.tags = {
            **(target.tags or {}),
            STAGE_TAG: stage.value,
            "stage_reason": reason[:256],
            "stage_at": _now(),
        }
        updated = ml.models.create_or_update(target)
        self.docs.append(
            f"{tenant.prefix}/registry/{name}/stages.jsonl",
            {"version": str(version), "stage": stage.value, "reason": reason, "at": _now()},
        )
        return self._to_version(name, updated)

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        return [self._to_version(name, m) for m in self._models(tenant, name)]

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        for v in reversed(self.versions(tenant, name)):
            if v.stage == Stage.LIVE:
                return v
        return None

    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path:
        """The version's files under `into`; returns the directory holding `metadata.json`
        (Azure ML nests the upload under the model name and the source folder's name)."""
        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        self.clients.ml.models.download(
            name=asset_name(tenant, version.name), version=version.version, download_path=into
        )
        found = sorted(into.rglob("metadata.json"), key=lambda p: len(p.parts))
        return found[0].parent if found else into


def registry_from_env() -> AzureMLRegistry:
    """The registry the pipeline's register step uses (`NW_PIPELINE_REGISTRY`), built from the
    `NW_AZURE_*` environment the pipeline passes to the step."""
    settings = Settings(track=Track.AZURE)
    cfg = AzureConfig.from_settings(settings)
    return AzureMLRegistry(cfg, AzureClients(cfg))


# ----- PipelineRunner: Azure ML pipelines -------------------------------------------------


class AzureMLPipelineRunner:
    """Azure ML pipeline jobs built by `nw.pipelines.azureml` from the shared steps. `submit`
    builds the job for the commit that submits it (there is no stored definition to upsert),
    in the experiment `<prefix>-<pipeline>`, on serverless compute; the tickets, the
    production summary and the run tree default to the workspace datastore
    (`deployed_defaults`)."""

    def __init__(
        self,
        cfg: AzureConfig,
        clients: AzureClients,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.sleep = sleep
        self.poll_s = poll_s

    def deployed_defaults(self, tenant: Tenant, pipeline: str) -> dict[str, str]:
        """Where the deploy puts the tickets and the production summaries on the workspace's
        default datastore, and where the tenant's run trees go."""
        from nw.pipelines import canonical
        from nw.pipelines.params import BASELINES

        kind = canonical(pipeline)
        return {
            "data_uri": self.cfg.datastore_uri("tickets/tickets.jsonl"),
            "production_summary": self.cfg.datastore_uri(f"{BASELINES}/{kind}_production.json"),
            "output_root": self.cfg.datastore_uri(f"{tenant.prefix}/pipelines/runs"),
        }

    def pipeline_config(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> Any:
        from nw.pipelines.azureml import AzureMLConfig

        image = (
            params.get("image_uri")
            or self.cfg.images.get("pipelines")
            or (f"{self.cfg.acr_server}/nw-pipelines:latest" if self.cfg.acr_server else "")
        )
        if not image:
            raise ValueError("no nw-pipelines image: set NW_AZURE_IMAGE_PIPELINES or NW_AZURE_ACR")
        return AzureMLConfig(
            tenant=tenant,
            image=str(image),
            identity_client_id=self.cfg.pipeline_identity_client_id or None,
            platform_env=self.cfg.platform_env(),
            defaults=self.deployed_defaults(tenant, pipeline),
        )

    def job(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any] | None = None) -> Any:
        """The `PipelineJob`, built locally; what `submit` sends."""
        from nw.pipelines.azureml import definition

        values = {
            k: v for k, v in (params or {}).items() if k not in ("template_path", "image_uri")
        }
        return definition(pipeline, self.pipeline_config(tenant, pipeline, params or {}), values)

    def submit(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> PipelineRun:
        from nw.pipelines import canonical

        job = self.job(tenant, pipeline, params)
        created = self.clients.ml.jobs.create_or_update(
            job, experiment_name=tenant.resource(canonical(pipeline))
        )
        return PipelineRun(
            pipeline=pipeline,
            run_id=created.name,
            status=JOB_STATES.get(str(created.status), RunStatus.QUEUED),
            url=getattr(created, "studio_url", None),
        )

    def status(self, tenant: Tenant, run: PipelineRun) -> PipelineRun:
        job = self.clients.ml.jobs.get(run.run_id)
        status = JOB_STATES.get(str(job.status), RunStatus.RUNNING)
        outputs: dict[str, str] = {}
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED):
            for child in self.clients.ml.jobs.list(parent_job_name=run.run_id):
                label = getattr(child, "display_name", None) or child.name
                outputs[label] = str(child.status)
        return PipelineRun(
            pipeline=run.pipeline,
            run_id=run.run_id,
            status=status,
            url=run.url or getattr(job, "studio_url", None),
            outputs=outputs,
        )

    def wait(self, tenant: Tenant, run: PipelineRun, timeout_s: float = 1800) -> PipelineRun:
        deadline = time.monotonic() + timeout_s
        while True:
            current = self.status(tenant, run)
            if current.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED):
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{run.run_id} still {current.status} after {timeout_s} s")
            self.sleep(self.poll_s)

    def logs(self, tenant: Tenant, run: PipelineRun) -> Iterator[str]:
        """`jobs.stream`, which prints the run's log to stdout until the run ends, captured
        line by line. Following a running job blocks until it finishes."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.clients.ml.jobs.stream(run.run_id)
        yield from buffer.getvalue().splitlines()


# ----- EndpointClient: Azure ML managed online endpoints ----------------------------------


class AzureMLEndpointClient:
    """Managed online endpoints, created on first deploy. A tenant's endpoint
    `nw-<tenant>-<name>-<h5>` runs one small `blue` deployment that each deploy replaces. The
    live endpoint `nw-live-<name>-<h5>` runs blue and green: `deploy(live=True,
    canary_percent=N)` puts the version on the colour that is not serving and sends it N
    percent of the traffic (100 when 0); `promote` moves the rest and removes the old colour,
    `rollback` sends everything back.

    The deployment is the course's own serving image as a custom container (nw/serving/vertex.py:
    `/health` and `/predict` on 8080 once `AIP_HTTP_PORT` is set), the registered model mounted
    at /var/nw/model and handed to the service as `NW_MODEL_URI`. The endpoint authenticates
    with its key, so the predict route is open inside the container.

    Data collection (Azure ML model data collector, `azure.ai.ml.entities.DataCollector`,
    learn.microsoft.com/azure/machine-learning/how-to-collect-production-data, checked
    2026-09-29 against azure-ai-ml 1.35): every live deployment carries it, a tenant's only
    when `NW_AZURE_TENANT_DATA_COLLECTION` is set. Four collections are enabled: `model_inputs`
    and `model_outputs`, the names model monitoring picks up as its production data assets
    (filled when the serving code logs through `azureml-ai-monitoring`'s `Collector`), and
    `request` and `response`, payload logging, which the collector fills for a custom container
    with no code change. Data lands in `workspaceblobstore` under
    `modelDataCollector/<endpoint>/<deployment>/<collection>/`, partitioned by hour."""

    COLLECTIONS = ("model_inputs", "model_outputs", "request", "response")

    def __init__(self, cfg: AzureConfig, clients: AzureClients) -> None:
        self.cfg = cfg
        self.clients = clients

    def _data_collector(self, live: bool) -> Any | None:
        """The deployment's `data_collector`, or None for a tenant that has not opted in."""
        if not (live or self.cfg.tenant_data_collection):
            return None
        from azure.ai.ml.entities import DataCollector, DeploymentCollection

        return DataCollector(
            collections={c: DeploymentCollection(enabled="true") for c in self.COLLECTIONS},
            rolling_rate="hour",
            sampling_rate=self.cfg.data_sampling_rate,
        )

    def endpoint(self, tenant: Tenant, name: str, live: bool = False) -> str:
        owner = LIVE if (live or tenant.name == LIVE) else tenant.name
        return endpoint_name(owner, name, self.cfg.scope)

    def _ensure_endpoint(self, endpoint: str, tenant: Tenant, name: str) -> Any:
        ml = self.clients.ml
        try:
            return ml.online_endpoints.get(endpoint)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "ResourceNotFoundError":
                raise
        from azure.ai.ml.entities import ManagedOnlineEndpoint

        created = ManagedOnlineEndpoint(
            name=endpoint,
            auth_mode="key",
            description=f"Northwind {name} for {tenant.prefix}",
            tags={"tenant": tenant.name, "environment": tenant.environment, "project": name},
        )
        return ml.online_endpoints.begin_create_or_update(created).result()

    def _deployment(
        self, tenant: Tenant, version: ModelVersion, endpoint: str, color: str, live: bool
    ) -> Any:
        from azure.ai.ml.entities import Environment, ManagedOnlineDeployment

        env = {
            "AIP_HTTP_PORT": str(SERVING_PORT),
            "AIP_HEALTH_ROUTE": INFERENCE_CONFIG["liveness_route"]["path"],
            "AIP_PREDICT_ROUTE": INFERENCE_CONFIG["scoring_route"]["path"],
            "NW_MODEL_URI": f"file://{MODEL_MOUNT}",
            "NW_MODEL_VERSION": version.version,
            "NW_TRACK": "azure",
            "NW_TENANT": tenant.name,
            "NW_ENVIRONMENT": tenant.environment,
        }
        if self.cfg.appinsights_connection_string:
            env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = self.cfg.appinsights_connection_string
        return ManagedOnlineDeployment(
            name=color,
            endpoint_name=endpoint,
            model=f"azureml:{asset_name(tenant, version.name)}:{version.version}",
            environment=Environment(
                image=self.cfg.image_for(version.name), inference_config=dict(INFERENCE_CONFIG)
            ),
            environment_variables=env,
            instance_type=self.cfg.live_endpoint_sku if live else self.cfg.endpoint_sku,
            instance_count=1,
            model_mount_path=MODEL_MOUNT,
            data_collector=self._data_collector(live),
            tags={"model_version": version.version, "tenant": tenant.name},
        )

    def deploy(
        self, tenant: Tenant, version: ModelVersion, *, live: bool = False, canary_percent: int = 0
    ) -> str:
        if not 0 <= canary_percent <= 100:
            raise ValueError(f"canary_percent {canary_percent}: 0 to 100")
        ml = self.clients.ml
        name = self.endpoint(tenant, version.name, live)
        endpoint = self._ensure_endpoint(name, tenant, version.name)
        traffic = {k: int(v) for k, v in (getattr(endpoint, "traffic", None) or {}).items()}
        serving = max(traffic, key=lambda k: traffic[k]) if any(traffic.values()) else None
        if live and serving:
            color = next(c for c in DEPLOYMENT_COLORS if c != serving)
        else:
            color = serving or DEPLOYMENT_COLORS[0]
        ml.online_deployments.begin_create_or_update(
            self._deployment(tenant, version, name, color, live)
        ).result()
        percent = canary_percent or 100
        if live and serving and serving != color and percent < 100:
            endpoint.traffic = {color: percent, serving: 100 - percent}
        else:
            endpoint.traffic = {
                color: 100,
                **({serving: 0} if serving and serving != color else {}),
            }
        updated = ml.online_endpoints.begin_create_or_update(endpoint).result()
        return getattr(updated, "scoring_uri", None) or name

    def set_traffic(
        self, tenant: Tenant, name: str, traffic: Mapping[str, int]
    ) -> Mapping[str, int]:
        if sum(traffic.values()) not in (0, 100):
            raise ValueError(f"traffic must add up to 100: {dict(traffic)}")
        ml = self.clients.ml
        endpoint = ml.online_endpoints.get(self.endpoint(tenant, name))
        endpoint.traffic = dict(traffic)
        ml.online_endpoints.begin_create_or_update(endpoint).result()
        return dict(traffic)

    def promote(self, tenant: Tenant, name: str) -> str:
        """The canary takes all the traffic and the old colour is deleted."""
        ml = self.clients.ml
        endpoint_id = self.endpoint(tenant, name)
        endpoint = ml.online_endpoints.get(endpoint_id)
        traffic = {k: int(v) for k, v in (endpoint.traffic or {}).items()}
        if len(traffic) < 2:
            return next(iter(traffic), "")
        canary = min(traffic, key=lambda k: traffic[k])
        old = max(traffic, key=lambda k: traffic[k])
        self.set_traffic(tenant, name, {canary: 100, old: 0})
        ml.online_deployments.begin_delete(name=old, endpoint_name=endpoint_id).result()
        return canary

    def rollback(self, tenant: Tenant, name: str) -> str:
        """Everything back to the colour with the most traffic; the canary is deleted."""
        ml = self.clients.ml
        endpoint_id = self.endpoint(tenant, name)
        endpoint = ml.online_endpoints.get(endpoint_id)
        traffic = {k: int(v) for k, v in (endpoint.traffic or {}).items()}
        if len(traffic) < 2:
            return next(iter(traffic), "")
        stable = max(traffic, key=lambda k: traffic[k])
        canary = min(traffic, key=lambda k: traffic[k])
        self.set_traffic(tenant, name, {stable: 100, canary: 0})
        ml.online_deployments.begin_delete(name=canary, endpoint_name=endpoint_id).result()
        return stable

    def invoke(self, tenant: Tenant, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ml = self.clients.ml
        endpoint_id = self.endpoint(tenant, name)
        endpoint = ml.online_endpoints.get(endpoint_id)
        key = ml.online_endpoints.get_keys(endpoint_id).primary_key
        body = dict(payload)
        deployment = body.pop("deployment", None)
        body.pop("path", None)
        instances = body.get("instances") or [body]
        headers = {"authorization": f"Bearer {key}"}
        if deployment:
            headers["azureml-model-deployment"] = str(deployment)
        r = self.clients.http.post(
            endpoint.scoring_uri, json={"instances": list(instances)}, headers=headers
        )
        r.raise_for_status()
        return r.json()

    def status(self, tenant: Tenant, name: str) -> Mapping[str, Any]:
        ml = self.clients.ml
        endpoint_id = self.endpoint(tenant, name)
        try:
            endpoint = ml.online_endpoints.get(endpoint_id)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError":
                return {"kind": "azureml-online-endpoint", "name": endpoint_id, "state": "absent"}
            raise
        deployments = [
            {
                "name": d.name,
                "model": getattr(d, "model", None),
                "instance_type": getattr(d, "instance_type", None),
                "state": getattr(d, "provisioning_state", None),
                "model_version": (getattr(d, "tags", None) or {}).get("model_version"),
            }
            for d in ml.online_deployments.list(endpoint_name=endpoint_id)
        ]
        return {
            "kind": "azureml-online-endpoint",
            "name": endpoint_id,
            "scoring_uri": getattr(endpoint, "scoring_uri", None),
            "traffic": dict(getattr(endpoint, "traffic", None) or {}),
            "deployments": deployments,
        }

    def delete(self, tenant: Tenant, name: str) -> None:
        """A tenant's endpoint is deleted; the live endpoint keeps its name and loses its
        deployments, so its scoring URI survives for the next promotion."""
        ml = self.clients.ml
        endpoint_id = self.endpoint(tenant, name)
        if tenant.name != LIVE:
            ml.online_endpoints.begin_delete(name=endpoint_id).result()
            return
        endpoint = ml.online_endpoints.get(endpoint_id)
        endpoint.traffic = {}
        ml.online_endpoints.begin_create_or_update(endpoint).result()
        for d in list(ml.online_deployments.list(endpoint_name=endpoint_id)):
            ml.online_deployments.begin_delete(name=d.name, endpoint_name=endpoint_id).result()


# ----- PromptStore: blob store of record plus an Azure ML data asset per version ----------


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class AzurePromptStore:
    """Microsoft Foundry has no prompt registry API today (fetched 2026-09-29: prompt agents
    are versioned as agents, and Microsoft's prompt versioning guidance is Git), so a prompt
    version is two things: the text and its record in the artifacts container
    (`<prefix>/prompts/<name>/<sha>.txt`, `<sha>.json`, `index.json` with the stage map), and an
    Azure ML data asset `northwind-<tenant>-prompt-<name>` whose version is the twelve-character
    SHA-256, tagged `sha256_12` and `stage`, pointing at the text, so prompts sit beside the
    models in the studio with lineage to the runs that used them. The version id is the same
    `prompt_version` that travels in answers and spans."""

    def __init__(self, cfg: AzureConfig, clients: AzureClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_container)

    def _dir(self, tenant: Tenant, name: str) -> str:
        return f"{tenant.prefix}/prompts/{name}"

    def _record(self, doc: Mapping[str, Any]) -> PromptVersion:
        return PromptVersion(
            name=doc["name"],
            version=doc["version"],
            text=doc["text"],
            sha256_12=doc["sha256_12"],
            stage=Stage(doc.get("stage", Stage.CANDIDATE.value)),
            tags=dict(doc.get("tags", {})),
        )

    def _asset(
        self, tenant: Tenant, name: str, sha: str, url: str, stage: Stage, tags: Mapping[str, str]
    ) -> None:
        from azure.ai.ml.constants import AssetTypes
        from azure.ai.ml.entities import Data

        self.clients.ml.data.create_or_update(
            Data(
                name=asset_name(tenant, f"prompt-{name}"),
                version=sha,
                path=url,
                type=AssetTypes.URI_FILE,
                description=f"prompt {name} for {tenant.prefix}",
                tags={
                    **{k: str(v) for k, v in tags.items()},
                    "sha256_12": sha,
                    STAGE_TAG: stage.value,
                    "prompt": name,
                    "tenant": tenant.name,
                },
            )
        )

    def register(
        self, tenant: Tenant, name: str, text: str, tags: Mapping[str, str]
    ) -> PromptVersion:
        sha = prompt_hash(text)
        directory = self._dir(tenant, name)
        existing = self.docs.read(f"{directory}/{sha}.json")
        if existing:
            return self._record(existing)
        url = self.docs.write_text(f"{directory}/{sha}.txt", text)
        self._asset(tenant, name, sha, url, Stage.CANDIDATE, tags)
        doc = {
            "name": name,
            "version": sha,
            "text": text,
            "sha256_12": sha,
            "stage": Stage.CANDIDATE.value,
            "tags": dict(tags),
            "tenant": tenant.name,
            "environment": tenant.environment,
            "registered": _now(),
            "asset": f"azureml:{asset_name(tenant, f'prompt-{name}')}:{sha}",
            "text_url": url,
        }
        self.docs.write(f"{directory}/{sha}.json", doc)
        index = self.docs.read(f"{directory}/index.json", {"versions": {}})
        index["versions"][sha] = Stage.CANDIDATE.value
        self.docs.write(f"{directory}/index.json", index)
        return self._record(doc)

    def get(self, tenant: Tenant, name: str, version: str | None = None) -> PromptVersion:
        directory = self._dir(tenant, name)
        index = self.docs.read(f"{directory}/index.json")
        if not index or not index.get("versions"):
            raise KeyError(f"no prompt {tenant.resource(name)}")
        if version is None:
            stages = index["versions"]
            for stage in (Stage.LIVE, Stage.APPROVED, Stage.CANDIDATE):
                hits = [v for v, s in stages.items() if s == stage.value]
                if hits:
                    version = hits[-1]
                    break
            if version is None:
                version = list(stages)[-1]
        doc = self.docs.read(f"{directory}/{version}.json")
        if not doc:
            raise KeyError(f"prompt {tenant.resource(name)} has no version {version}")
        return self._record(doc)

    def set_stage(self, tenant: Tenant, name: str, version: str, stage: Stage) -> PromptVersion:
        directory = self._dir(tenant, name)
        doc = self.docs.read(f"{directory}/{version}.json")
        if not doc:
            raise KeyError(f"prompt {tenant.resource(name)} has no version {version}")
        index = self.docs.read(f"{directory}/index.json", {"versions": {}})
        changed: dict[str, Stage] = {version: stage}
        if stage in STEP_BACK:
            for v, s in list(index["versions"].items()):
                if s == stage.value and v != version:
                    changed[v] = STEP_BACK[stage]
        for v, new in changed.items():
            record = doc if v == version else self.docs.read(f"{directory}/{v}.json")
            if not record:
                continue
            record["stage"] = new.value
            index["versions"][v] = new.value
            self.docs.write(f"{directory}/{v}.json", record)
            self._asset(tenant, name, v, record.get("text_url") or "", new, record.get("tags", {}))
        self.docs.write(f"{directory}/index.json", index)
        return self._record(doc)

    def versions(self, tenant: Tenant, name: str) -> Sequence[PromptVersion]:
        directory = self._dir(tenant, name)
        index = self.docs.read(f"{directory}/index.json")
        if not index:
            return []
        out = []
        for v in index.get("versions", {}):
            doc = self.docs.read(f"{directory}/{v}.json")
            if doc:
                out.append(self._record(doc))
        return out


# ----- VectorStore: Azure AI Search -------------------------------------------------------


def document_key(doc_id: str) -> str:
    """AI Search keys take letters, digits, `_`, `-` and `=`; chunk ids have `:` and `/`."""
    return base64.urlsafe_b64encode(doc_id.encode("utf-8")).decode("ascii")


class AISearchVectorStore:
    """An index per tenant and collection (`northwind-alice-policies`), created on first
    upsert: the chunk's text (searchable), its id and metadata, and a vector field with an HNSW
    profile whose vectorizer is the Foundry embedding deployment, so a query without a vector is
    embedded by the service (integrated vectorization). `search` is hybrid: BM25 over the text
    and the vector query, fused by reciprocal rank. Upserts without vectors are embedded here
    through the same deployment on the Foundry v1 endpoint."""

    VECTOR = "vector"
    PROFILE = "nw-hnsw-profile"
    VECTORIZER = "nw-foundry"

    def __init__(self, cfg: AzureConfig, clients: AzureClients, batch: int = 500) -> None:
        self.cfg = cfg
        self.clients = clients
        self.batch = batch

    def index_definition(self, name: str) -> Any:
        from azure.search.documents.indexes.models import (
            AzureOpenAIVectorizer,
            AzureOpenAIVectorizerParameters,
            HnswAlgorithmConfiguration,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        return SearchIndex(
            name=name,
            fields=[
                SimpleField(name="key", type=SearchFieldDataType.STRING, key=True),
                SimpleField(name="id", type=SearchFieldDataType.STRING, filterable=True),
                SearchableField(name="text", type=SearchFieldDataType.STRING),
                SimpleField(name="metadata", type=SearchFieldDataType.STRING),
                SearchField(
                    name=self.VECTOR,
                    type="Collection(Edm.Single)",
                    searchable=True,
                    vector_search_dimensions=self.cfg.embedding_dimensions,
                    vector_search_profile_name=self.PROFILE,
                ),
            ],
            vector_search=VectorSearch(
                algorithms=[HnswAlgorithmConfiguration(name="nw-hnsw")],
                profiles=[
                    VectorSearchProfile(
                        name=self.PROFILE,
                        algorithm_configuration_name="nw-hnsw",
                        vectorizer_name=self.VECTORIZER,
                    )
                ],
                vectorizers=[
                    AzureOpenAIVectorizer(
                        vectorizer_name=self.VECTORIZER,
                        parameters=AzureOpenAIVectorizerParameters(
                            resource_url=self.cfg.foundry_root,
                            deployment_name=self.cfg.embedding_deployment,
                            model_name=self.cfg.embedding_deployment,
                        ),
                    )
                ],
            ),
        )

    def _ensure(self, index: str) -> None:
        self.clients.search_index.create_or_update_index(self.index_definition(index))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embeddings from the Foundry deployment, 16 texts a request."""
        from nw.llm.providers.azure_foundry import openai_url

        url = f"{openai_url(self.cfg.foundry_root)}/embeddings"
        headers = {"authorization": f"Bearer {self.clients.token(SCOPE)}"}
        out: list[list[float]] = []
        for start in range(0, len(texts), 16):
            chunk = list(texts[start : start + 16])
            r = self.clients.http.post(
                url, json={"model": self.cfg.embedding_deployment, "input": chunk}, headers=headers
            )
            r.raise_for_status()
            rows = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend([list(map(float, d["embedding"])) for d in rows])
        return out

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int:
        index = search_index_name(tenant, collection)
        self._ensure(index)
        vecs = [list(v) for v in vectors] if vectors is not None else self.embed(texts)
        docs = [
            {
                "key": document_key(doc_id),
                "id": doc_id,
                "text": text,
                "metadata": json.dumps(dict(metadata[i]) if i < len(metadata) else {}),
                self.VECTOR: vecs[i],
            }
            for i, (doc_id, text) in enumerate(zip(ids, texts, strict=True))
        ]
        client = self.clients.search(index)
        for start in range(0, len(docs), self.batch):
            client.merge_or_upload_documents(documents=docs[start : start + self.batch])
        return len(docs)

    def search(
        self,
        tenant: Tenant,
        collection: str,
        query: str,
        k: int = 8,
        vector: Sequence[float] | None = None,
    ) -> Sequence[Hit]:
        from azure.search.documents.models import VectorizableTextQuery, VectorizedQuery

        vq: Any = (
            VectorizedQuery(vector=list(vector), k_nearest_neighbors=k, fields=self.VECTOR)
            if vector is not None
            else VectorizableTextQuery(text=query, k_nearest_neighbors=k, fields=self.VECTOR)
        )
        results = self.clients.search(search_index_name(tenant, collection)).search(
            search_text=query, vector_queries=[vq], top=k, select=["id", "text", "metadata"]
        )
        hits = []
        for r in results:
            try:
                meta = json.loads(r.get("metadata") or "{}")
            except ValueError:
                meta = {}
            hits.append(
                Hit(
                    id=r["id"],
                    text=r["text"],
                    score=float(r.get("@search.score") or 0.0),
                    metadata=meta,
                )
            )
        return hits

    def count(self, tenant: Tenant, collection: str) -> int:
        try:
            return int(
                self.clients.search(search_index_name(tenant, collection)).get_document_count()
            )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError":
                return 0
            raise

    def drop(self, tenant: Tenant, collection: str) -> None:
        """The index belongs to the tenant and is created on upsert, so it is deleted."""
        self.clients.search_index.delete_index(search_index_name(tenant, collection))


# ----- AgentRuntime: Foundry Agent Service hosted agents ----------------------------------


class FoundryHostedAgentRuntime:
    """The agent runtime of the Azure track, split by owner:

    - A tenant's agent runs as a Foundry Agent Service hosted agent, registered in the Foundry
      project (this class: `deploy`, `invoke`, `status`). A hosted agent endpoint serves one
      version at a time with no traffic split, which a tenant does not need.
    - The live agent runs on Container Apps (`northwind-live-agent`, `deploy/azure/modules/
      agents.bicep`) in multiple-revision mode, because the promotion drill needs a canary:
      `scripts/deploy_azure.sh release` adds a revision at 10 percent and `approve` moves it to
      100. It is registered too: `register(Tenant("live"), card)` (`NW_TENANT=live make
      agent-cards PUSH=1` after the approval) writes its card with runtime `container-apps` and
      the app's name into the same registry document. `deploy` refuses the live owner, so the
      live agent never has a second, unsplit runtime beside the app.

    The template also gives every tenant `-policy`, `-agent` and `-mcp` Container Apps: the
    service layer the parts run against over HTTP. The hosted agent is the managed runtime slot.

    A hosted agent: the nw-agent image in ACR becomes an immutable
    agent version of `northwind-<tenant>-agent` with the Invocations protocol, a sandbox of one
    vCPU and 2 GiB, the environment of the deploy, and the guardrail (`rai_config`, the Foundry
    content-safety policy `NW_AZURE_RAI_POLICY`) reading the task on the way in and the answer
    on the way out. The agent endpoint is the Foundry project's
    `/agents/<name>/endpoint/protocols/invocations`; a session id goes in the
    `agent_session_id` query parameter. Every version carries `agent_version`, the tenant and
    the image in its metadata, which is what the project's agent list shows; the full agent
    card is kept in `agents/agents.json` in the artifacts container, as on Google Cloud."""

    REGISTRY_PATH = "agents/agents.json"

    def __init__(
        self,
        cfg: AzureConfig,
        clients: AzureClients,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 5.0,
        timeout_s: float = 600.0,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_container)
        self.sleep = sleep
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    def agent_name(self, tenant: Tenant) -> str:
        return tenant.resource("agent")[: LIMITS["agent"]]

    def definition(self, tenant: Tenant, image: str, env: Mapping[str, str]) -> Any:
        from azure.ai.projects.models import (
            AgentEndpointProtocol,
            ContainerConfiguration,
            HostedAgentDefinition,
            ProtocolVersionRecord,
            RaiConfig,
            RaiInvocationModeration,
        )

        rai = None
        if self.cfg.rai_policy:
            rai = RaiConfig(
                rai_policy_name=self.cfg.rai_policy,
                invocations_moderation=RaiInvocationModeration(
                    response_mode="non_streaming",
                    input_paths=["$.task"],
                    output_paths=["$.final"],
                ),
            )
        return HostedAgentDefinition(
            protocol_versions=[
                ProtocolVersionRecord(
                    protocol=AgentEndpointProtocol.INVOCATIONS, version=AGENT_PROTOCOL_VERSION
                )
            ],
            cpu="1",
            memory="2Gi",
            container_configuration=ContainerConfiguration(image=image),
            environment_variables=dict(env),
            rai_config=rai,
        )

    def deploy(self, tenant: Tenant, image: str, env: Mapping[str, str], *, version: str) -> str:
        if tenant.name == LIVE:
            raise ValueError(
                "the live agent runs on Container Apps with a traffic split: "
                "scripts/deploy_azure.sh release, then approve"
            )
        merged = {k: str(v) for k, v in env.items()}
        merged.update(
            {
                "NW_AGENT_VERSION": version,
                "NW_TRACK": "azure",
                "PORT": str(AGENT_PORT),
            }
        )
        merged.setdefault("NW_TENANT", tenant.name)
        merged.setdefault("NW_ENVIRONMENT", tenant.environment)
        # The model route comes from the platform, so a caller that passes only the gateway key
        # (or an empty NW_GATEWAY_URL, which means LiteLLM and is not set on this track) still
        # deploys an agent that reaches its models: API Management first, else Foundry itself.
        for var, value in (
            ("NW_AZURE_APIM_GATEWAY_URL", self.cfg.apim_gateway_url),
            ("NW_AZURE_FOUNDRY_ENDPOINT", self.cfg.foundry_endpoint),
            ("APPLICATIONINSIGHTS_CONNECTION_STRING", self.cfg.appinsights_connection_string),
        ):
            if value and not merged.get(var):
                merged[var] = value
        if not merged.get("NW_GATEWAY_URL"):
            merged.pop("NW_GATEWAY_URL", None)
        name = self.agent_name(tenant)
        agents = self.clients.projects.agents
        created = agents.create_version(
            agent_name=name,
            definition=self.definition(tenant, image, merged),
            metadata={"agent_version": version, "tenant": tenant.name, "image": image[:512]},
            description=f"Northwind resolver of tenant {tenant.name}",
        )
        state = self._wait_active(name, str(created.version))
        resource = f"{self.cfg.project_endpoint}/agents/{name}/versions/{created.version}"
        self._upsert_registry(
            tenant,
            {
                "id": name,
                "resource": resource,
                "image": image,
                "agent_version": version,
                "foundry_version": str(created.version),
                "status": state,
                "deployed": _now(),
            },
        )
        return resource

    def _wait_active(self, name: str, version: str) -> str:
        deadline = time.monotonic() + self.timeout_s
        while True:
            info = self.clients.projects.agents.get_version(agent_name=name, agent_version=version)
            status = str(_field(info, "status") or "")
            if status == "active":
                return status
            if status == "failed":
                raise RuntimeError(
                    f"agent {name} version {version} failed: {_field(info, 'error')}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"agent {name} version {version} still {status!r}")
            self.sleep(self.poll_s)

    def invoke(
        self, tenant: Tenant, payload: Mapping[str, Any], *, session_id: str | None = None
    ) -> Mapping[str, Any]:
        url = (
            f"{self.cfg.project_endpoint}/agents/{self.agent_name(tenant)}"
            "/endpoint/protocols/invocations"
        )
        params = {"api-version": "v1"}
        if session_id:
            params["agent_session_id"] = session_id
        r = self.clients.http.post(
            url,
            params=params,
            json=dict(payload),
            headers={"authorization": f"Bearer {self.clients.token(SCOPE)}"},
        )
        r.raise_for_status()
        return r.json()

    def register(self, tenant: Tenant, card: Mapping[str, Any]) -> str:
        live = tenant.name == LIVE
        entry = {
            "id": card.get("id") or tenant.resource(card.get("name", "agent")),
            "tenant": tenant.name,
            "runtime": "container-apps" if live else "foundry-hosted-agent",
            **({"app": tenant.resource("agent")} if live else {}),
            "registered": _now(),
            **{k: v for k, v in card.items() if k != "id"},
        }
        return self._upsert_registry(tenant, entry)

    def status(self, tenant: Tenant) -> Mapping[str, Any]:
        name = self.agent_name(tenant)
        agents = self.clients.projects.agents
        try:
            versions = list(agents.list_versions(agent_name=name))
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError":
                return {"name": name, "state": "absent"}
            raise
        if not versions:
            return {"name": name, "state": "absent"}
        latest = max(versions, key=lambda v: _version_key(str(_field(v, "version"))))
        registry = self.docs.read(self.REGISTRY_PATH, {"agents": []})
        card = next(
            (
                a
                for a in registry.get("agents", [])
                if a.get("tenant") == tenant.name and a.get("id") == name
            ),
            None,
        )
        metadata = _field(latest, "metadata") or {}
        return {
            "name": name,
            "state": str(_field(latest, "status") or ""),
            "foundry_version": str(_field(latest, "version")),
            "agent_version": metadata.get("agent_version", ""),
            "image": metadata.get("image", ""),
            "versions": len(versions),
            "registry": card,
        }

    def _upsert_registry(self, tenant: Tenant, entry: Mapping[str, Any]) -> str:
        entry = {**entry, "tenant": tenant.name}
        registry = self.docs.read(
            self.REGISTRY_PATH, {"environment": tenant.environment, "agents": []}
        )
        agents = registry.get("agents", [])
        previous = next(
            (a for a in agents if a.get("tenant") == tenant.name and a.get("id") == entry["id"]),
            {},
        )
        kept = [
            a for a in agents if not (a.get("tenant") == tenant.name and a.get("id") == entry["id"])
        ]
        kept.append({**previous, **entry})
        registry["agents"] = sorted(kept, key=lambda a: (a.get("tenant", ""), a.get("id", "")))
        registry["updated"] = _now()
        return self.docs.write(self.REGISTRY_PATH, registry)


def _field(obj: Any, name: str) -> Any:
    """SDK models are both attribute and mapping objects; fakes are either."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


# ----- factory ----------------------------------------------------------------------------


@dataclass
class AzurePlatform(Platform):
    """`Platform` plus the config, for scripts that need the names or the endpoints."""

    cfg: AzureConfig = field(default=None)  # type: ignore[assignment]


def build(settings: Settings, clients: AzureClients | None = None) -> AzurePlatform:
    cfg = AzureConfig.from_settings(settings)
    clients = clients or AzureClients(cfg)
    return AzurePlatform(
        track=Track.AZURE,
        registry=AzureMLRegistry(cfg, clients),
        pipelines=AzureMLPipelineRunner(cfg, clients),
        endpoints=AzureMLEndpointClient(cfg, clients),
        prompts=AzurePromptStore(cfg, clients),
        vectors=AISearchVectorStore(cfg, clients),
        agents=FoundryHostedAgentRuntime(cfg, clients),
        gateway_url=cfg.gateway_url,
        cfg=cfg,
    )


# ----- CLI --------------------------------------------------------------------------------


def describe(settings: Settings | None = None, tenant: Tenant | None = None) -> list[str]:
    """The resolved configuration and the tenant's resource names. No Azure call."""
    from nw.platform.base import tenant_from_env

    settings = settings or Settings(track=Track.AZURE)
    cfg = AzureConfig.from_settings(settings)
    tenant = tenant or tenant_from_env(settings)
    lines = [
        f"{k:<30} {v}" for k, v in cfg.__dict__.items() if k != "appinsights_connection_string"
    ]
    insights = "set" if cfg.appinsights_connection_string else "unset"
    lines.append(f"{'appinsights_connection_string':<30} {insights}")
    lines.append("")
    lines.append(f"names for tenant {tenant.name} (scope hash {cfg.scope}):")
    lines += [f"  {k:<30} {v}" for k, v in names(tenant, cfg.scope).items()]
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "describe"
    if command == "describe":
        print("\n".join(describe()))
        return 0
    if command == "pipeline-definition":
        import yaml

        from nw.pipelines.azureml import as_dict
        from nw.platform.base import tenant_from_env

        settings = Settings(track=Track.AZURE)
        cfg = AzureConfig.from_settings(settings)
        runner = AzureMLPipelineRunner(cfg, AzureClients(cfg))
        tenant = tenant_from_env(settings)
        for name in argv[1:] or ["triage"]:
            print(yaml.safe_dump(as_dict(runner.job(tenant, name)), sort_keys=False))
        return 0
    print(
        "usage: python -m nw.platform.azure describe | pipeline-definition [triage|semantic ...]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
