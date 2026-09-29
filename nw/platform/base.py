"""The platform contract.

Six protocols, one value object and one factory. The protocols are deliberately small: the
course teaches the operations every platform has (register, approve, submit, invoke, retrieve,
deploy) and leaves vendor extras to the reference tab. Implementations live in
`nw/platform/aws.py`, `nw/platform/gcp.py` and `nw/platform/local.py`; each is imported lazily
so a track never needs the other tracks' SDKs installed.

Naming: every resource a tenant creates carries `Tenant.prefix` (cohort mode) or the empty
prefix (solo mode), so two learners on one platform never collide (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nw.config import Settings, Track

TENANT_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")


class Stage(StrEnum):
    """The approval state of a registered model or prompt version."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    LIVE = "live"
    RETIRED = "retired"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Tenant:
    """Who is acting on the platform. `name` is the learner's handle in cohort mode and
    `solo` in solo mode; `prefix` is what every resource name starts with."""

    name: str
    environment: str = "northwind"

    def __post_init__(self) -> None:
        if not TENANT_RE.match(self.name):
            raise ValueError(
                f"tenant name {self.name!r}: 2 to 16 lowercase letters or digits, "
                "starting with a letter"
            )

    @property
    def prefix(self) -> str:
        return f"{self.environment}-{self.name}"

    def resource(self, kind: str) -> str:
        """`northwind-alice-triage`: the name a resource of `kind` gets for this tenant."""
        return f"{self.prefix}-{kind}"


@dataclass
class ModelVersion:
    name: str
    version: str
    stage: Stage
    uri: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass
class PromptVersion:
    name: str
    version: str
    text: str
    sha256_12: str
    stage: Stage = Stage.CANDIDATE
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass
class PipelineRun:
    pipeline: str
    run_id: str
    status: RunStatus
    url: str | None = None
    outputs: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    text: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelRegistry(Protocol):
    """Register a trained artifact, move it through stages, find the live one.

    AWS: SageMaker Model Registry (model package groups, approval status).
    GCP: Model Registry with aliases. Local: MLflow registry with aliases."""

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
    ) -> ModelVersion: ...
    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion: ...
    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]: ...
    def live(self, tenant: Tenant, name: str) -> ModelVersion | None: ...
    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path: ...


@runtime_checkable
class PipelineRunner(Protocol):
    """Submit a compiled pipeline and follow it. The step code is shared across tracks;
    only the definition differs (SageMaker SDK on AWS, Kubeflow SDK on GCP and local).

    `pipeline` names the definition (`triage`, `semantic`, `retrain-triage`); the implementation
    resolves it to `<NW_PIPELINE_DIR>/<pipeline>.yaml` (Kubeflow) or the tenant's SageMaker
    pipeline, and `params["template_path"]` overrides that path and is not passed on as a
    parameter. Parameter names are the snake_case names in `nw/pipelines/params.py`."""

    def submit(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> PipelineRun: ...
    def status(self, tenant: Tenant, run: PipelineRun) -> PipelineRun: ...
    def wait(self, tenant: Tenant, run: PipelineRun, timeout_s: float = 1800) -> PipelineRun: ...
    def logs(self, tenant: Tenant, run: PipelineRun) -> Iterator[str]: ...


@runtime_checkable
class EndpointClient(Protocol):
    """Deploy a registered version to the tenant's endpoint and invoke it.

    AWS: Serverless Inference for tenants, a real-time endpoint with a CodeDeploy canary as the
    live target. GCP: Cloud Run from the registry artifact for tenants, a Vertex endpoint with a
    traffic split as the live target. Local: MLflow serving or BentoML, canary by replica weight."""

    def deploy(
        self, tenant: Tenant, version: ModelVersion, *, live: bool = False, canary_percent: int = 0
    ) -> str: ...
    def invoke(
        self, tenant: Tenant, name: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    def status(self, tenant: Tenant, name: str) -> Mapping[str, Any]: ...
    def delete(self, tenant: Tenant, name: str) -> None: ...


@runtime_checkable
class PromptStore(Protocol):
    """Prompts are versioned artifacts with a hash that travels in answers and spans.

    AWS: Bedrock Prompt Management. GCP: the Gen AI SDK prompt management. Local: the
    in-repo registry under `nw/llm/prompts` with MLflow as the store of record."""

    def register(
        self, tenant: Tenant, name: str, text: str, tags: Mapping[str, str]
    ) -> PromptVersion: ...
    def get(self, tenant: Tenant, name: str, version: str | None = None) -> PromptVersion: ...
    def set_stage(self, tenant: Tenant, name: str, version: str, stage: Stage) -> PromptVersion: ...
    def versions(self, tenant: Tenant, name: str) -> Sequence[PromptVersion]: ...


@runtime_checkable
class VectorStore(Protocol):
    """The managed retrieval behind the policy service's Retriever.

    AWS: Bedrock Knowledge Base on S3 Vectors. GCP: RAG Engine on Vector Search. Local: Qdrant."""

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int: ...
    def search(
        self,
        tenant: Tenant,
        collection: str,
        query: str,
        k: int = 8,
        vector: Sequence[float] | None = None,
    ) -> Sequence[Hit]: ...
    def count(self, tenant: Tenant, collection: str) -> int: ...
    def drop(self, tenant: Tenant, collection: str) -> None: ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Deploy the agent image and invoke it through the platform, with the registry entry.

    AWS: AgentCore Runtime, entered through the agent registry. GCP: Agent Engine.
    Local: the agent container from the compose stack."""

    def deploy(
        self, tenant: Tenant, image: str, env: Mapping[str, str], *, version: str
    ) -> str: ...
    def invoke(
        self, tenant: Tenant, payload: Mapping[str, Any], *, session_id: str | None = None
    ) -> Mapping[str, Any]: ...
    def register(self, tenant: Tenant, card: Mapping[str, Any]) -> str: ...
    def status(self, tenant: Tenant) -> Mapping[str, Any]: ...


@dataclass
class Platform:
    """Everything a track offers, resolved once from settings."""

    track: Track
    registry: ModelRegistry
    pipelines: PipelineRunner
    endpoints: EndpointClient
    prompts: PromptStore
    vectors: VectorStore
    agents: AgentRuntime
    gateway_url: str | None = None  # the model gateway every model call goes through (ADR 0008)

    def describe(self) -> dict[str, str]:
        return {
            "track": self.track.value,
            "registry": type(self.registry).__name__,
            "pipelines": type(self.pipelines).__name__,
            "endpoints": type(self.endpoints).__name__,
            "prompts": type(self.prompts).__name__,
            "vectors": type(self.vectors).__name__,
            "agents": type(self.agents).__name__,
            "gateway": self.gateway_url or "direct",
        }


def platform_for(settings: Settings) -> Platform:
    """The track's implementation, imported lazily so other tracks' SDKs are never required."""
    if settings.track == Track.AWS:
        from nw.platform.aws import build

        return build(settings)
    if settings.track == Track.GCP:
        from nw.platform.gcp import build

        return build(settings)
    if settings.track == Track.AZURE:
        from nw.platform.azure import build

        return build(settings)
    from nw.platform.local import build

    return build(settings)


def tenant_from_env(settings: Settings) -> Tenant:
    """`NW_TENANT` names the learner in cohort mode; `solo` when unset (ADR 0009)."""
    name = getattr(settings, "tenant", None) or "solo"
    environment = getattr(settings, "environment", None) or "northwind"
    return Tenant(name=name, environment=environment)
