"""The Google Cloud track of the platform contract (ADR 0008), on the Agent Platform
(formerly Vertex AI): Model Registry, Pipelines, Cloud Run and Vertex endpoints, prompt
management, RAG Engine and Agent Engine, behind the six protocols of `nw.platform.base`.

Every SDK is imported lazily through `GcpClients`, so the module imports on any track and
tests hand in fakes. Install the SDKs with `uv sync --extra platform-gcp`.

Naming follows `Tenant.prefix`: a tenant's model is `northwind-alice-triage` in the registry,
its Cloud Run service has the same name, its corpus `northwind-alice-policies`, its agent
`northwind-alice-agent`. The live target is `northwind-live-<model>` (a Vertex endpoint with
a traffic split) and the live Cloud Run services come from Cloud Deploy.

What the platform has no resource or API for is kept in the artifacts bucket:
- stages of prompt versions (prompt management has versions, not stages)
- the stage log of model versions (aliases hold `live` and `approved`; the log holds the
  reasons and the stages no alias can, `candidate` and `retired`)
- the agent registry document `agents/agents.json`
- the live endpoints' canary record `<environment>-live/endpoints/<name>.json`
- the RAG corpus's metadata `<prefix>/rag/<collection>/meta.json`

Documents shared by several writers (the agent registry, the stage logs, prompt indexes) are
written read-modify-write with a generation precondition (`if_generation_match`) and retried
on a conflict, so two learners of a cohort never overwrite each other's update.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nw.config import Settings, Track
from nw.platform.base import (
    STEP_BACK,
    UNIQUE_STAGES,
    Hit,
    ModelVersion,
    PipelineRun,
    Platform,
    PromptVersion,
    RunStatus,
    Stage,
    Tenant,
    clamp_score,
    default_prompt,
)

STAGE_ALIASES: tuple[str, ...] = tuple(s.value for s in Stage)
# Which of several stage aliases on one version wins when they disagree.
STAGE_PRIORITY: tuple[Stage, ...] = (Stage.LIVE, Stage.APPROVED, Stage.RETIRED, Stage.CANDIDATE)
# The route each course service answers on; `EndpointClient.invoke` posts the payload there.
SERVICE_PATHS: Mapping[str, str] = {
    "triage": "/triage",
    "semantic": "/classify",
    "policy": "/ask",
    "agent": "/route",
}
PIPELINE_STATES: Mapping[str, RunStatus] = {
    "PIPELINE_STATE_QUEUED": RunStatus.QUEUED,
    "PIPELINE_STATE_PENDING": RunStatus.QUEUED,
    "PIPELINE_STATE_RUNNING": RunStatus.RUNNING,
    "PIPELINE_STATE_SUCCEEDED": RunStatus.SUCCEEDED,
    "PIPELINE_STATE_FAILED": RunStatus.FAILED,
    "PIPELINE_STATE_CANCELLING": RunStatus.STOPPED,
    "PIPELINE_STATE_CANCELLED": RunStatus.STOPPED,
    "PIPELINE_STATE_PAUSED": RunStatus.QUEUED,
}
# The prebuilt serving container the registry attaches to an uploaded sklearn artifact so a
# version can be deployed to a Vertex endpoint; the course's own Cloud Run images serve the
# same artifact through NW_MODEL_URI.
DEFAULT_SERVING_IMAGE = "us-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-5:latest"
GCS_RE = re.compile(r"^gs://([^/]+)/?(.*)$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def split_gcs(uri: str) -> tuple[str, str]:
    """`gs://bucket/a/b` to `("bucket", "a/b")`."""
    m = GCS_RE.match(uri)
    if not m:
        raise ValueError(f"not a gs:// uri: {uri!r}")
    return m.group(1), m.group(2)


def _bucket_from_uri(uri: str) -> str:
    return split_gcs(uri)[0]


@dataclass
class GcpConfig:
    """Everything the implementations need, resolved once from settings and the environment.

    The platform region (`NW_GCP_RUN_REGION`, where Cloud Run, Pipelines, the registry and
    Agent Engine live) is separate from `NW_GCP_REGION`, which names the model endpoint
    region (`global` for the Anthropic models on Vertex)."""

    project: str
    region: str = "us-central1"
    environment: str = "northwind"
    artifacts_bucket: str = ""
    pipelines_bucket: str = ""
    data_bucket: str = ""
    pipeline_dir: str = "artifacts/pipelines"
    serving_image: str = DEFAULT_SERVING_IMAGE
    gateway_url: str | None = None
    endpoint_id_base: int = 100000
    live_models: tuple[str, ...] = ("triage", "semantic")

    @classmethod
    def from_settings(cls, settings: Settings, env: Mapping[str, str] | None = None) -> GcpConfig:
        e = os.environ if env is None else env
        project = getattr(settings, "gcp_project", None) or e.get("NW_GCP_PROJECT") or ""
        if not project:
            raise ValueError("NW_GCP_PROJECT is required on the gcp track")
        environment = (
            getattr(settings, "environment", None) or e.get("NW_ENVIRONMENT") or "northwind"
        )
        artifacts = e.get("NW_GCP_ARTIFACTS_BUCKET") or f"{environment}-{project}-artifacts"
        pipelines = e.get("NW_GCP_PIPELINES_BUCKET") or f"{environment}-{project}-pipelines"
        data = e.get("NW_GCP_DATA_BUCKET") or f"{environment}-{project}-data"
        return cls(
            project=project,
            region=e.get("NW_GCP_RUN_REGION")
            or getattr(settings, "gcp_platform_region", None)
            or "us-central1",
            environment=environment,
            artifacts_bucket=artifacts,
            pipelines_bucket=pipelines,
            data_bucket=data,
            pipeline_dir=e.get("NW_PIPELINE_DIR") or "artifacts/pipelines",
            serving_image=e.get("NW_GCP_SERVING_IMAGE") or DEFAULT_SERVING_IMAGE,
            gateway_url=getattr(settings, "gateway_url", None) or e.get("NW_GATEWAY_URL"),
            endpoint_id_base=int(e.get("NW_GCP_ENDPOINT_ID_BASE") or 100000),
            live_models=tuple((e.get("NW_GCP_LIVE_MODELS") or "triage,semantic").split(",")),
        )

    def live_endpoint_id(self, name: str) -> str:
        """The numeric id Terraform gave the live endpoint of `name` (modules/live)."""
        if name not in self.live_models:
            raise KeyError(f"{name!r} has no live endpoint; live models: {self.live_models}")
        return str(self.endpoint_id_base + self.live_models.index(name))

    def live_endpoint_name(self, name: str) -> str:
        return (
            f"projects/{self.project}/locations/{self.region}/endpoints/"
            f"{self.live_endpoint_id(name)}"
        )


class GcpClients:
    """Lazy handles on the SDKs. Each property imports on first use; tests replace the
    attributes with fakes before anything is called."""

    def __init__(self, cfg: GcpConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def set(self, **fakes: Any) -> GcpClients:
        self._cache.update(fakes)
        return self

    @property
    def aiplatform(self) -> Any:
        def make() -> Any:
            from google.cloud import aiplatform

            aiplatform.init(project=self.cfg.project, location=self.cfg.region)
            return aiplatform

        return self._get("aiplatform", make)

    @property
    def storage(self) -> Any:
        def make() -> Any:
            from google.cloud import storage

            return storage.Client(project=self.cfg.project)

        return self._get("storage", make)

    @property
    def logging(self) -> Any:
        def make() -> Any:
            from google.cloud import logging as cloud_logging

            return cloud_logging.Client(project=self.cfg.project)

        return self._get("logging", make)

    @property
    def run(self) -> Any:
        def make() -> Any:
            from google.cloud import run_v2

            return run_v2.ServicesClient()

        return self._get("run", make)

    @property
    def rag(self) -> Any:
        def make() -> Any:
            import vertexai
            from vertexai import rag

            vertexai.init(project=self.cfg.project, location=self.cfg.region)
            return rag

        return self._get("rag", make)

    @property
    def prompts(self) -> Any:
        """`vertexai.preview.prompts` (google-cloud-aiplatform), the SDK's prompt management;
        None when the SDK is not installed, in which case the bucket is the only store."""

        def make() -> Any:
            try:
                import vertexai
                from vertexai.preview import prompts
            except ImportError:
                return None
            vertexai.init(project=self.cfg.project, location=self.cfg.region)
            return prompts

        return self._get("prompts", make)

    @property
    def reasoning_engines(self) -> Any:
        def make() -> Any:
            from google.cloud import aiplatform_v1

            endpoint = f"{self.cfg.region}-aiplatform.googleapis.com"
            return aiplatform_v1.ReasoningEngineServiceClient(
                client_options={"api_endpoint": endpoint}
            )

        return self._get("reasoning_engines", make)

    @property
    def reasoning_engine_execution(self) -> Any:
        def make() -> Any:
            from google.cloud import aiplatform_v1

            endpoint = f"{self.cfg.region}-aiplatform.googleapis.com"
            return aiplatform_v1.ReasoningEngineExecutionServiceClient(
                client_options={"api_endpoint": endpoint}
            )

        return self._get("reasoning_engine_execution", make)

    @property
    def http(self) -> Any:
        def make() -> Any:
            import httpx

            return httpx.Client(timeout=120)

        return self._get("http", make)


# ----- bucket helpers ---------------------------------------------------------------------------


class Conflict(RuntimeError):
    """A generation precondition kept failing: another writer is updating the document."""


def _precondition_failed(exc: BaseException) -> bool:
    return type(exc).__name__ == "PreconditionFailed" or getattr(exc, "code", None) == 412


class Documents:
    """JSON documents in the artifacts bucket, the store for everything the platform has no
    resource for. Updates of a shared document go through `update`, which reads the object's
    generation and writes with `if_generation_match` (0 for an object that must not exist yet),
    retrying from a fresh read when another writer got there first."""

    ATTEMPTS = 8

    def __init__(self, clients: GcpClients, bucket: str) -> None:
        self.clients = clients
        self.bucket = bucket

    def _blob(self, path: str) -> Any:
        return self.clients.storage.bucket(self.bucket).blob(path)

    def read(self, path: str, default: Any = None) -> Any:
        blob = self._blob(path)
        if not blob.exists():
            return default
        return json.loads(blob.download_as_text())

    def _read_versioned(self, path: str) -> tuple[Any, str | None, int]:
        blob = self._blob(path)
        if not blob.exists():
            return blob, None, 0
        blob.reload()
        generation = int(getattr(blob, "generation", 0) or 0)
        return blob, blob.download_as_text(if_generation_match=generation), generation

    def update_text(self, path: str, mutate: Callable[[str | None], str], content_type: str) -> str:
        for _ in range(self.ATTEMPTS):
            blob, text, generation = self._read_versioned(path)
            new = mutate(text)
            try:
                blob.upload_from_string(
                    new, content_type=content_type, if_generation_match=generation
                )
                return new
            except Exception as exc:  # noqa: BLE001  PreconditionFailed without the import
                if not _precondition_failed(exc):
                    raise
        raise Conflict(f"gs://{self.bucket}/{path} kept changing under {self.ATTEMPTS} attempts")

    def update(self, path: str, mutate: Callable[[Any], Any], default: Any) -> Any:
        """Read, mutate a copy, write back only if nobody wrote in between; returns the doc."""
        out: dict[str, Any] = {}

        def apply(text: str | None) -> str:
            doc = json.loads(text) if text is not None else copy.deepcopy(default)
            out["doc"] = mutate(doc)
            return json.dumps(out["doc"], indent=2, sort_keys=True)

        self.update_text(path, apply, "application/json")
        return out["doc"]

    def write(self, path: str, doc: Any) -> str:
        """A document only its owner writes (one version's record): a plain overwrite."""
        self._blob(path).upload_from_string(
            json.dumps(doc, indent=2, sort_keys=True), content_type="application/json"
        )
        return f"gs://{self.bucket}/{path}"

    def append(self, path: str, row: Mapping[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True) + "\n"
        self.update_text(path, lambda text: (text or "") + line, "application/x-ndjson")

    def upload_dir(self, local: Path, prefix: str) -> str:
        bucket = self.clients.storage.bucket(self.bucket)
        for p in sorted(local.rglob("*")):
            if p.is_file():
                bucket.blob(f"{prefix}/{p.relative_to(local).as_posix()}").upload_from_filename(
                    str(p)
                )
        return f"gs://{self.bucket}/{prefix}"

    def upload_text(self, path: str, text: str, content_type: str = "text/plain") -> str:
        self._blob(path).upload_from_string(text, content_type=content_type)
        return f"gs://{self.bucket}/{path}"

    def delete_prefix(self, prefix: str) -> int:
        bucket = self.clients.storage.bucket(self.bucket)
        found = list(bucket.list_blobs(prefix=prefix))
        for blob in found:
            blob.delete()
        return len(found)

    def download_prefix(self, uri: str, into: Path) -> Path:
        bucket_name, prefix = split_gcs(uri)
        bucket = self.clients.storage.bucket(bucket_name)
        into.mkdir(parents=True, exist_ok=True)
        for blob in bucket.list_blobs(prefix=prefix.rstrip("/") + "/"):
            rel = blob.name[len(prefix.rstrip("/")) + 1 :]
            if not rel:
                continue
            target = into / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
        return into


# ----- ModelRegistry ------------------------------------------------------------------------


def _stage_from_aliases(aliases: Sequence[str]) -> Stage:
    present = set(aliases)
    for stage in STAGE_PRIORITY:
        if stage.value in present:
            return stage
    return Stage.CANDIDATE


def _stage_of(aliases: Sequence[str], logged: str | None) -> Stage:
    """A version's stage: the `live` or `approved` alias when it holds one (aliases are unique
    per model, so there is one holder of each); else the last stage the log recorded for it,
    where a `live` or `approved` that lost its alias to a newer holder reads as stepped back."""
    present = set(aliases)
    for stage in UNIQUE_STAGES:
        if stage.value in present:
            return stage
    if logged in {s.value for s in Stage}:
        stage = Stage(logged)
        return STEP_BACK.get(stage, stage)
    if Stage.RETIRED.value in present:
        return Stage.RETIRED
    return Stage.CANDIDATE


def _labels(tenant: Tenant, tags: Mapping[str, str]) -> dict[str, str]:
    """Vertex labels: lowercase letters, digits, underscore and dash, 63 characters."""
    out = {"tenant": tenant.name, "environment": tenant.environment}
    for k, v in tags.items():
        key = re.sub(r"[^a-z0-9_-]", "_", k.lower())[:63]
        out[key] = re.sub(r"[^a-z0-9_-]", "_", str(v).lower())[:63]
    return out


class VertexModelRegistry:
    """Model Registry: one model per tenant and name, one version per registration. `live`
    and `approved` are version aliases (unique per model, so the alias itself is the single
    holder); `candidate` and `retired`, which many versions hold at once, live in the stage log
    `<prefix>/registry/<name>/stages.jsonl`, which also records the reason of every change.
    Moving `live` or `approved` onto a version takes the alias off the previous holder and logs
    its step back (`retired`, `candidate`), the rules of `nw.platform.base`. Metrics travel in
    the version description."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

    def _parent(self, tenant: Tenant, name: str) -> Any | None:
        display = tenant.resource(name)
        found = self.clients.aiplatform.Model.list(filter=f'display_name="{display}"')
        return found[0] if found else None

    def _log_path(self, tenant: Tenant, name: str) -> str:
        return f"{tenant.prefix}/registry/{name}/stages.jsonl"

    def _logged(self, tenant: Tenant, name: str) -> dict[str, str]:
        """The last stage the log recorded per version."""
        blob = self.docs._blob(self._log_path(tenant, name))
        if not blob.exists():
            return {}
        out: dict[str, str] = {}
        for line in blob.download_as_text().splitlines():
            if line.strip():
                row = json.loads(line)
                out[str(row.get("version"))] = str(row.get("stage"))
        return out

    def _log(self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str) -> None:
        self.docs.append(
            self._log_path(tenant, name),
            {"version": version, "stage": stage.value, "reason": reason, "at": _now()},
        )

    def _to_version(
        self, name: str, model: Any, version: Any | None = None, logged: str | None = None
    ) -> ModelVersion:
        v = version if version is not None else model
        aliases = list(getattr(v, "version_aliases", None) or [])
        desc = getattr(v, "version_description", None) or ""
        metrics: dict[str, float] = {}
        recorded_uri = ""
        try:
            described = json.loads(desc)
            metrics = {k: float(x) for k, x in described.get("metrics", {}).items()}
            recorded_uri = str(described.get("artifact_uri") or "")
        except (ValueError, AttributeError):
            pass
        return ModelVersion(
            name=name,
            version=str(getattr(v, "version_id", "")),
            stage=_stage_of(aliases, logged),
            # each version's own artifact (the parent Model object is the default version)
            uri=recorded_uri or getattr(v, "uri", None) or getattr(model, "uri", None) or "",
            metrics=metrics,
            tags=dict(getattr(model, "labels", None) or {}),
        )

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
    ) -> ModelVersion:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        uri = self.docs.upload_dir(artifact, f"{tenant.prefix}/models/{name}/{stamp}")
        parent = self._parent(tenant, name)
        model = self.clients.aiplatform.Model.upload(
            display_name=tenant.resource(name),
            artifact_uri=uri,
            serving_container_image_uri=self.cfg.serving_image,
            parent_model=parent.resource_name if parent is not None else None,
            is_default_version=False,
            version_aliases=[],
            version_description=json.dumps(
                {"metrics": dict(metrics), "registered": _now(), "artifact_uri": uri}
            ),
            labels=_labels(tenant, tags),
        )
        out = self._to_version(name, model)
        out.uri = uri
        out.metrics = dict(metrics)
        out.stage = Stage.CANDIDATE
        self._log(tenant, name, out.version, Stage.CANDIDATE, "registered")
        return out

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        parent = self._parent(tenant, name)
        if parent is None:
            raise KeyError(f"no model {tenant.resource(name)} in the registry")
        registry = parent.versioning_registry
        listed = list(registry.list_versions())
        current = [v for v in listed if str(getattr(v, "version_id", "")) == version]
        if not current:
            raise KeyError(f"{tenant.resource(name)} has no version {version}")
        if stage in UNIQUE_STAGES:
            for other in listed:
                other_id = str(getattr(other, "version_id", ""))
                if other_id != version and stage.value in (other.version_aliases or []):
                    registry.remove_version_aliases(
                        version_name=other_id, alias_names=[stage.value]
                    )
                    self._log(
                        tenant, name, other_id, STEP_BACK[stage], f"replaced by {version}: {reason}"
                    )
        old = [a for a in (current[0].version_aliases or []) if a in STAGE_ALIASES]
        if old:
            registry.remove_version_aliases(version_name=version, alias_names=old)
        if stage in UNIQUE_STAGES:
            registry.add_version_aliases(version_name=version, alias_names=[stage.value])
        self._log(tenant, name, version, stage, reason)
        refreshed = [
            v for v in registry.list_versions() if str(getattr(v, "version_id", "")) == version
        ]
        out = self._to_version(name, parent, refreshed[0] if refreshed else current[0], stage.value)
        out.stage = stage
        return out

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        """Every version, oldest first."""
        parent = self._parent(tenant, name)
        if parent is None:
            return []
        logged = self._logged(tenant, name)
        found = [
            self._to_version(name, parent, v, logged.get(str(getattr(v, "version_id", ""))))
            for v in parent.versioning_registry.list_versions()
        ]
        return sorted(found, key=lambda v: int(v.version) if v.version.isdigit() else 0)

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        for v in reversed(list(self.versions(tenant, name))):
            if v.stage == Stage.LIVE:
                return v
        return None

    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path:
        uri = version.uri
        if not uri:
            parent = self._parent(tenant, version.name)
            if parent is None:
                raise KeyError(f"no model {tenant.resource(version.name)}")
            uri = self.clients.aiplatform.Model(f"{parent.resource_name}@{version.version}").uri
        return self.docs.download_prefix(uri, into)


# ----- PipelineRunner -----------------------------------------------------------------------------


class VertexPipelineRunner:
    """Vertex AI Pipelines from a compiled Kubeflow YAML. `pipeline` names the file
    `<NW_PIPELINE_DIR>/<pipeline>.yaml` (or `params["template_path"]`, local or gs://); the
    run goes to the tenant's pipeline root as the tenant's pipelines service account.

    A named template is uploaded first to `gs://<artifacts>/<prefix>/pipelines/<pipeline>.yaml`,
    the object the tenant's Cloud Scheduler job reads for `retrain-triage`, so the weekly run and
    the last hand submission use one definition. `upload` alone is `make pipeline-upload-gcp`.

    Every submit (and every `upload`) ships the checkout's code (`nw.pipelines.source`): the
    bundle goes to `gs://<artifacts>/<prefix>/source/nw-source-<sha>.tar.gz` and, for the
    scheduler, `source/latest.tar.gz`; the run gets its URI as `source_uri`, which the
    components read through the `/gcs/` mount. `platform_env` tells the gate's champion lookup
    and the register step that they are on this project's platform."""

    def __init__(
        self,
        cfg: GcpConfig,
        clients: GcpClients,
        sleep: Callable[[float], None] = time.sleep,
        bundler: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.sleep = sleep
        self.bundler = bundler

    def source_uri(self, tenant: Tenant, name: str) -> str:
        return f"gs://{self.cfg.artifacts_bucket}/{tenant.prefix}/source/{name}"

    def ship_source(self, tenant: Tenant) -> str:
        """Upload the checkout's source bundle (and `latest.tar.gz`); returns its URI."""
        from nw.pipelines.source import default_bundle

        bundle = (self.bundler or default_bundle)()
        bucket = self.clients.storage.bucket(self.cfg.artifacts_bucket)
        for name in (bundle.name, "latest.tar.gz"):
            blob = bucket.blob(f"{tenant.prefix}/source/{name}")
            if name == bundle.name and blob.exists():
                continue
            blob.upload_from_filename(str(bundle.path))
        return self.source_uri(tenant, bundle.name)

    def platform_env(self, tenant: Tenant) -> str:
        """The `NW_*` settings a Vertex step needs to build this platform, as the JSON the
        launcher's `--env` takes."""
        return json.dumps(
            {
                "NW_TRACK": "gcp",
                "NW_GCP_PROJECT": self.cfg.project,
                "NW_GCP_RUN_REGION": self.cfg.region,
                "NW_GCP_ARTIFACTS_BUCKET": self.cfg.artifacts_bucket,
                "NW_GCP_PIPELINES_BUCKET": self.cfg.pipelines_bucket,
                "NW_GCP_DATA_BUCKET": self.cfg.data_bucket,
                "NW_ENVIRONMENT": tenant.environment,
                "NW_TENANT": tenant.name,
            },
            sort_keys=True,
        )

    def _local(self, pipeline: str) -> Path:
        path = Path(self.cfg.pipeline_dir) / f"{pipeline}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"no compiled pipeline at {path}; run `make pipeline-compile` first"
            )
        return path

    def template_uri(self, tenant: Tenant, pipeline: str) -> str:
        """Where the tenant's compiled `pipeline` lives in the artifacts bucket."""
        return f"gs://{self.cfg.artifacts_bucket}/{tenant.prefix}/pipelines/{pipeline}.yaml"

    def upload(self, tenant: Tenant, pipeline: str, *, ship: bool = True) -> str:
        """Copy `<NW_PIPELINE_DIR>/<pipeline>.yaml` to `template_uri`, and for `triage` or
        `semantic` to the scheduler's name too (`retrain-triage.yaml`); returns the URI of
        `pipeline`."""
        from nw.pipelines import ALIASES, aliases_of

        local = self._local(pipeline)
        names = [pipeline] + ([] if pipeline in ALIASES else aliases_of(pipeline))
        for name in names:
            bucket, blob = split_gcs(self.template_uri(tenant, name))
            self.clients.storage.bucket(bucket).blob(blob).upload_from_filename(str(local))
        if ship:  # the scheduler's `source/latest.tar.gz` follows the template
            self.ship_source(tenant)
        return self.template_uri(tenant, pipeline)

    def _template(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> str:
        explicit = params.get("template_path")
        if explicit:
            return str(explicit)
        return self.upload(tenant, pipeline, ship=False)

    def _url(self, resource_name: str) -> str:
        run_id = resource_name.rsplit("/", 1)[-1]
        return (
            f"https://console.cloud.google.com/vertex-ai/locations/{self.cfg.region}/"
            f"pipelines/runs/{run_id}?project={self.cfg.project}"
        )

    def service_account(self, tenant: Tenant) -> str:
        return f"nw-{tenant.name}-pipelines@{self.cfg.project}.iam.gserviceaccount.com"

    def deployed_defaults(self, tenant: Tenant, pipeline: str) -> dict[str, str]:
        """The platform's locations for the parameters whose defaults are repo paths, the same
        values the tenant's Cloud Scheduler job passes: the tickets in the data bucket, the
        tenant's run tree in the artifacts bucket (a component reads `gs://` as the `/gcs/`
        mount), and the production summary the deploy copied to `gs://<artifacts>/baselines/`.
        A bucket the config does not know leaves the repo default in place."""
        from nw.pipelines import canonical
        from nw.pipelines.params import baseline_uri

        out: dict[str, str] = {}
        if self.cfg.data_bucket:
            out["data_uri"] = f"gs://{self.cfg.data_bucket}/tickets/tickets.jsonl"
        if self.cfg.artifacts_bucket:
            out["output_root"] = f"gs://{self.cfg.artifacts_bucket}/{tenant.prefix}/pipelines/runs"
            out["production_summary"] = baseline_uri(
                "gs", self.cfg.artifacts_bucket, canonical(pipeline)
            )
        return out

    def submit(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> PipelineRun:
        values = {k: v for k, v in params.items() if k != "template_path"}
        values.setdefault("tenant", tenant.name)
        values.setdefault("environment", tenant.environment)
        for key, value in self.deployed_defaults(tenant, pipeline).items():
            values.setdefault(key, value)
        template = self._template(tenant, pipeline, params)
        if not values.get("source_uri"):
            values["source_uri"] = self.ship_source(tenant)
        values.setdefault("platform_env", self.platform_env(tenant))
        job = self.clients.aiplatform.PipelineJob(
            display_name=tenant.resource(pipeline),
            template_path=template,
            pipeline_root=f"gs://{self.cfg.pipelines_bucket}/{tenant.prefix}",
            parameter_values=values,
            enable_caching=False,
            labels={"tenant": tenant.name, "environment": tenant.environment},
        )
        job.submit(service_account=self.service_account(tenant))
        return PipelineRun(
            pipeline=pipeline,
            run_id=job.resource_name,
            status=PIPELINE_STATES.get(_state_name(job.state), RunStatus.QUEUED),
            url=self._url(job.resource_name),
        )

    def status(self, tenant: Tenant, run: PipelineRun) -> PipelineRun:
        job = self.clients.aiplatform.PipelineJob.get(run.run_id)
        return PipelineRun(
            pipeline=run.pipeline,
            run_id=run.run_id,
            status=PIPELINE_STATES.get(_state_name(job.state), RunStatus.RUNNING),
            url=run.url or self._url(run.run_id),
            outputs=dict(run.outputs),
        )

    def wait(self, tenant: Tenant, run: PipelineRun, timeout_s: float = 1800) -> PipelineRun:
        deadline = time.monotonic() + timeout_s
        current = self.status(tenant, run)
        while current.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"pipeline run {run.run_id} still {current.status} after {timeout_s}s"
                )
            self.sleep(30)
            current = self.status(tenant, run)
        return current

    def logs(self, tenant: Tenant, run: PipelineRun) -> Iterator[str]:
        run_id = run.run_id.rsplit("/", 1)[-1]
        flt = (
            'resource.type="aiplatform.googleapis.com/PipelineJob" '
            f'AND resource.labels.pipeline_job_id="{run_id}"'
        )
        for entry in self.clients.logging.list_entries(filter_=flt, order_by="timestamp asc"):
            payload = getattr(entry, "payload", None)
            if isinstance(payload, Mapping):
                text = payload.get("message") or json.dumps(dict(payload), sort_keys=True)
            else:
                text = str(payload)
            yield f"{getattr(entry, 'timestamp', '')} {text}"


def _state_name(state: Any) -> str:
    return getattr(state, "name", None) or str(state)


# ----- EndpointClient ----------------------------------------------------------------------------


class CloudRunEndpointClient:
    """Tenant serving is the tenant's Cloud Run service reading the version's artifact through
    NW_MODEL_URI; the live target is the Vertex endpoint `northwind-live-<name>` with a traffic
    split. `deploy(live=False)` rewrites the service's environment (a new revision). The live
    Cloud Run services move through Cloud Deploy (`make release-gcp`), not through this client.

    The live endpoint holds at most two deployed models, the stable one and one canary, and the
    record `<environment>-live/endpoints/<name>.json` says which is which (explicit, never
    guessed from the traffic):

    - `deploy(live=True, canary_percent=N)` deploys the version as the canary at N percent;
      the stable model keeps the rest. A canary that never finished is undeployed first.
    - `deploy(live=True, canary_percent=0 or 100)`, or `promote`, finishes: the version (the
      canary, when it is the one on the endpoint already, never a second copy) takes all the
      traffic and every other deployed model is undeployed, so no replica keeps billing.
    - `rollback` sends everything back to the stable model and undeploys the canary."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients, api_key: str | None = None) -> None:
        self.cfg = cfg
        self.clients = clients
        self.api_key = api_key or os.environ.get("NW_API_KEY")
        self.docs = Documents(clients, cfg.artifacts_bucket)

    # ----- the live endpoint's canary record

    def _record_path(self, name: str) -> str:
        return f"{self.cfg.environment}-live/endpoints/{name}.json"

    def live_record(self, name: str) -> dict[str, Any]:
        return self.docs.read(self._record_path(name), {"stable": None, "canary": None})

    def _set_record(self, name: str, stable: str | None, canary: str | None, note: str) -> None:
        def mutate(doc: dict[str, Any]) -> dict[str, Any]:
            history = list(doc.get("history", []))[-19:]
            history.append({"stable": stable, "canary": canary, "note": note, "at": _now()})
            return {"stable": stable, "canary": canary, "history": history}

        self.docs.update(self._record_path(name), mutate, {"stable": None, "canary": None})

    def _deployed(self, endpoint: Any) -> dict[str, Any]:
        return {str(m.id): m for m in endpoint.list_models()}

    def _finish(self, name: str, endpoint: Any, keep: str, note: str) -> str:
        """`keep` takes all the traffic; every other deployed model is undeployed."""
        endpoint.update(traffic_split={keep: 100})
        for other in list(self._deployed(endpoint)):
            if other != keep:
                endpoint.undeploy(deployed_model_id=other)
        self._set_record(name, keep, None, note)
        return keep

    def promote(self, tenant: Tenant, name: str) -> str:
        """The canary takes all the traffic; the old stable model is undeployed."""
        endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
        record = self.live_record(name)
        deployed = self._deployed(endpoint)
        canary = record.get("canary")
        if not canary or canary not in deployed:
            raise KeyError(f"no canary on the live {name} endpoint to promote")
        return self._finish(name, endpoint, canary, f"promoted by {tenant.name}")

    def rollback(self, tenant: Tenant, name: str) -> str:
        """Everything back to the stable model; the canary is undeployed."""
        endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
        record = self.live_record(name)
        deployed = self._deployed(endpoint)
        stable = record.get("stable")
        if not stable or stable not in deployed:
            raise KeyError(f"no stable model on the live {name} endpoint to roll back to")
        return self._finish(name, endpoint, stable, f"rolled back by {tenant.name}")

    def _deploy_live(self, tenant: Tenant, version: ModelVersion, canary_percent: int) -> str:
        if not 0 <= canary_percent <= 100:
            raise ValueError(f"canary_percent {canary_percent}: 0 to 100")
        name = version.name
        endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
        display = f"{tenant.resource(name)}-v{version.version}"
        record = self.live_record(name)
        deployed = self._deployed(endpoint)
        percent = canary_percent or 100
        existing = next((i for i, m in deployed.items() if m.display_name == display), None)
        stable = record.get("stable") if record.get("stable") in deployed else None
        if stable is None and deployed:
            split = {str(k): int(v) for k, v in (endpoint.traffic_split or {}).items()}
            stable = max(deployed, key=lambda i: split.get(i, 0))
        if existing is not None:
            if percent >= 100:
                self._finish(name, endpoint, existing, f"{display} finished by {tenant.name}")
            elif existing != stable and stable is not None:
                endpoint.update(traffic_split={existing: percent, stable: 100 - percent})
                self._set_record(name, stable, existing, f"{display} at {percent}")
            return endpoint.resource_name
        canary = record.get("canary")
        if canary and canary in deployed and canary != stable:
            # one canary at a time: the unfinished one goes before the next is deployed
            if stable is not None:
                endpoint.update(traffic_split={stable: 100})
            endpoint.undeploy(deployed_model_id=canary)
        model = self._model_resource(tenant, version)
        model.deploy(
            endpoint=endpoint,
            deployed_model_display_name=display,
            traffic_percentage=percent if stable is not None else 100,
            machine_type="n1-standard-2",
            min_replica_count=1,
            max_replica_count=1,
        )
        new = next(
            (i for i, m in self._deployed(endpoint).items() if m.display_name == display), None
        )
        if new is None:
            raise RuntimeError(f"{display} did not appear on the live {name} endpoint")
        if stable is None or percent >= 100:
            self._finish(name, endpoint, new, f"{display} deployed by {tenant.name}")
        else:
            self._set_record(name, stable, new, f"{display} canary at {percent}")
        return endpoint.resource_name

    def _service_path(self, tenant: Tenant, name: str) -> str:
        return (
            f"projects/{self.cfg.project}/locations/{self.cfg.region}/services/"
            f"{tenant.resource(name)}"
        )

    def _model_resource(self, tenant: Tenant, version: ModelVersion) -> Any:
        found = self.clients.aiplatform.Model.list(
            filter=f'display_name="{tenant.resource(version.name)}"'
        )
        if not found:
            raise KeyError(f"no model {tenant.resource(version.name)} in the registry")
        return self.clients.aiplatform.Model(f"{found[0].resource_name}@{version.version}")

    def deploy(
        self, tenant: Tenant, version: ModelVersion, *, live: bool = False, canary_percent: int = 0
    ) -> str:
        if live:
            return self._deploy_live(tenant, version, canary_percent)
        service = self.clients.run.get_service(name=self._service_path(tenant, version.name))
        container = service.template.containers[0]
        env = [e for e in container.env if e.name not in ("NW_MODEL_URI", "NW_MODEL_VERSION")]
        env.append(_run_env("NW_MODEL_URI", version.uri))
        env.append(_run_env("NW_MODEL_VERSION", version.version))
        container.env = env
        op = self.clients.run.update_service(service=service)
        updated = op.result() if hasattr(op, "result") else op
        return getattr(updated, "uri", "") or service.uri

    def invoke(self, tenant: Tenant, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if name in self.cfg.live_models and tenant.name == "live":
            endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
            instances = payload.get("instances") or [dict(payload)]
            resp = endpoint.predict(instances=list(instances))
            return {
                "predictions": list(resp.predictions),
                "deployed_model_id": getattr(resp, "deployed_model_id", None),
            }
        service = self.clients.run.get_service(name=self._service_path(tenant, name))
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        path = payload.get("path") or SERVICE_PATHS.get(name, "/predict")
        body = {k: v for k, v in payload.items() if k != "path"}
        r = self.clients.http.post(service.uri + path, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def status(self, tenant: Tenant, name: str) -> Mapping[str, Any]:
        if tenant.name == "live" and name in self.cfg.live_models:
            endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
            record = self.live_record(name)
            return {
                "kind": "vertex-endpoint",
                "name": endpoint.resource_name,
                "stable": record.get("stable"),
                "canary": record.get("canary"),
                "traffic_split": dict(endpoint.traffic_split or {}),
                "deployed_models": [
                    {"id": m.id, "display_name": m.display_name, "model": m.model}
                    for m in endpoint.list_models()
                ],
            }
        service = self.clients.run.get_service(name=self._service_path(tenant, name))
        env = {e.name: e.value for e in service.template.containers[0].env}
        return {
            "kind": "cloud-run",
            "name": service.name,
            "url": service.uri,
            "revision": getattr(service, "latest_ready_revision", ""),
            "model_uri": env.get("NW_MODEL_URI", ""),
            "model_version": env.get("NW_MODEL_VERSION", ""),
        }

    def delete(self, tenant: Tenant, name: str) -> None:
        if tenant.name == "live" and name in self.cfg.live_models:
            endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(name))
            endpoint.undeploy_all()
            self._set_record(name, None, None, f"emptied by {tenant.name}")
            return
        op = self.clients.run.delete_service(name=self._service_path(tenant, name))
        if hasattr(op, "result"):
            op.result()


def _run_env(name: str, value: str) -> Any:
    try:
        from google.cloud import run_v2

        return run_v2.EnvVar(name=name, value=value)
    except ImportError:  # tests without the SDK
        from types import SimpleNamespace

        return SimpleNamespace(name=name, value=value)


# ----- PromptStore ---------------------------------------------------------------------------


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class VertexPromptStore:
    """Prompt management on the Agent Platform through `vertexai.preview.prompts`
    (google-cloud-aiplatform; the online prompt store, preview). Its versions have ids but no
    stages, so every registration also writes `<prefix>/prompts/<name>/<sha>.json` with the
    stage and tags, and `index.json` with the stage map (updated with a generation
    precondition); the version id the course uses is the twelve-character SHA-256 of the text,
    the same `prompt_version` that travels in answers. Stages follow `nw.platform.base`.
    Without the SDK the bucket alone is the store, and the record says so."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

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

    def register(
        self, tenant: Tenant, name: str, text: str, tags: Mapping[str, str]
    ) -> PromptVersion:
        sha = prompt_hash(text)
        directory = self._dir(tenant, name)
        existing = self.docs.read(f"{directory}/{sha}.json")
        if existing:
            return self._record(existing)
        index = self.docs.read(f"{directory}/index.json", {"prompt_id": None, "versions": {}})
        online: dict[str, Any] = {"store": "bucket"}
        prompts = self.clients.prompts
        if prompts is not None:
            prompt = prompts.Prompt(prompt_data=text, prompt_name=tenant.resource(name))
            created = prompts.create_version(
                prompt=prompt, prompt_id=index.get("prompt_id"), version_name=sha
            )
            index["prompt_id"] = getattr(created, "prompt_id", index.get("prompt_id"))
            online = {
                "store": "vertexai.preview.prompts",
                "prompt_id": index["prompt_id"],
                "version_id": getattr(created, "version_id", None),
            }
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
            "online": online,
        }
        self.docs.write(f"{directory}/{sha}.json", doc)
        prompt_id = index.get("prompt_id")

        def add(current: dict[str, Any]) -> dict[str, Any]:
            current.setdefault("versions", {})[sha] = Stage.CANDIDATE.value
            order = current.setdefault("order", list(current["versions"]))
            if sha not in order:
                order.append(sha)
            current["prompt_id"] = current.get("prompt_id") or prompt_id
            return current

        self.docs.update(f"{directory}/index.json", add, {"prompt_id": None, "versions": {}})
        return self._record(doc)

    def get(self, tenant: Tenant, name: str, version: str | None = None) -> PromptVersion:
        directory = self._dir(tenant, name)
        if version is None:
            found = self.versions(tenant, name)
            if not found:
                raise KeyError(f"no prompt {tenant.resource(name)}")
            return default_prompt(found)
        doc = self.docs.read(f"{directory}/{version}.json")
        if not doc:
            raise KeyError(f"prompt {tenant.resource(name)} has no version {version}")
        return self._record(doc)

    def set_stage(self, tenant: Tenant, name: str, version: str, stage: Stage) -> PromptVersion:
        directory = self._dir(tenant, name)
        doc = self.docs.read(f"{directory}/{version}.json")
        if not doc:
            raise KeyError(f"prompt {tenant.resource(name)} has no version {version}")
        changed: dict[str, str] = {}

        def move(index: dict[str, Any]) -> dict[str, Any]:
            changed.clear()
            versions = index.setdefault("versions", {})
            if stage in UNIQUE_STAGES:
                # one version holds the stage at a time: the previous holder steps back
                for v, held in list(versions.items()):
                    if held == stage.value and v != version:
                        versions[v] = changed[v] = STEP_BACK[stage].value
            versions[version] = changed[version] = stage.value
            return index

        self.docs.update(f"{directory}/index.json", move, {"prompt_id": None, "versions": {}})
        for v, value in changed.items():
            record = doc if v == version else self.docs.read(f"{directory}/{v}.json")
            if record:
                record["stage"] = value
                self.docs.write(f"{directory}/{v}.json", record)
        return self._record(doc)

    def versions(self, tenant: Tenant, name: str) -> Sequence[PromptVersion]:
        """Every version, oldest first (the index keeps the registration order)."""
        directory = self._dir(tenant, name)
        index = self.docs.read(f"{directory}/index.json")
        if not index:
            return []
        order = list(index.get("order") or [])
        order += [v for v in index.get("versions", {}) if v not in order]
        out = []
        for v in order:
            doc = self.docs.read(f"{directory}/{v}.json")
            if doc:
                out.append(self._record(doc))
        return out


# ----- VectorStore ---------------------------------------------------------------------------


class RagEngineVectorStore:
    """RAG Engine: the corpus `northwind-<tenant>-<collection>` (Terraform creates
    `policies`), files imported from the artifacts bucket, retrieval through
    `retrieval_query`. RAG Engine chunks and embeds on import, so `vectors` is ignored.

    Layout under `<prefix>/rag/<collection>/`: `docs/<id>.txt` is what the corpus imports (the
    import path is `docs/`, so nothing else becomes a document), and `meta.json` maps every id to
    the metadata given to `upsert`. A hit's id comes from its source file's name, and its
    metadata from `meta.json`, so `audience` and `current` reach the policy service's filter on
    every hit; a hit whose id has no metadata carries none, and the retriever treats that as
    internal. `drop` deletes the corpus files and the bucket objects, so the next upsert cannot
    import stale documents back.

    Scores: RAG Engine's context `score` is a vector distance for the default distance metric
    (`NW_GCP_RAG_SCORE=distance`, lower is closer): the hit's score is `1 - distance`, clamped
    to the contract's 0 to 1. `NW_GCP_RAG_SCORE=similarity` passes a similarity through."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients, score_kind: str | None = None) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)
        self.score_kind = (score_kind or os.environ.get("NW_GCP_RAG_SCORE") or "distance").lower()
        if self.score_kind not in ("distance", "similarity"):
            raise ValueError(f"NW_GCP_RAG_SCORE={self.score_kind!r}: distance or similarity")

    def _corpus(self, tenant: Tenant, collection: str, create: bool = False) -> Any:
        rag = self.clients.rag
        display = tenant.resource(collection)
        for corpus in rag.list_corpora():
            if corpus.display_name == display:
                return corpus
        if not create:
            raise KeyError(f"no RAG corpus {display}; `make deploy-gcp` creates it")
        return rag.create_corpus(display_name=display)

    def _prefix(self, tenant: Tenant, collection: str) -> str:
        return f"{tenant.prefix}/rag/{collection}"

    @staticmethod
    def file_name(doc_id: str) -> str:
        """Chunk ids carry `#` and `/`; the file name is the id, percent-encoded."""
        return urllib.parse.quote(doc_id, safe="") + ".txt"

    @staticmethod
    def id_of(uri: str) -> str:
        return urllib.parse.unquote(uri.rsplit("/", 1)[-1].removesuffix(".txt"))

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int:
        if not (len(ids) == len(texts) == len(metadata)):
            raise ValueError("ids, texts and metadata must have the same length")
        corpus = self._corpus(tenant, collection, create=True)
        prefix = self._prefix(tenant, collection)
        for doc_id, text in zip(ids, texts, strict=True):
            self.docs.upload_text(f"{prefix}/docs/{self.file_name(doc_id)}", text)
        given = {doc_id: dict(meta) for doc_id, meta in zip(ids, metadata, strict=True)}

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            current.update(given)
            return current

        self.docs.update(f"{prefix}/meta.json", merge, {})
        rag = self.clients.rag
        rag.import_files(
            corpus_name=corpus.name,
            paths=[f"gs://{self.cfg.artifacts_bucket}/{prefix}/docs/"],
            transformation_config=rag.TransformationConfig(
                chunking_config=rag.ChunkingConfig(chunk_size=512, chunk_overlap=64)
            ),
            max_embedding_requests_per_min=600,
        )
        return len(ids)

    def _score(self, raw: float) -> float:
        return clamp_score(1.0 - raw if self.score_kind == "distance" else raw)

    def search(
        self,
        tenant: Tenant,
        collection: str,
        query: str,
        k: int = 8,
        vector: Sequence[float] | None = None,
    ) -> Sequence[Hit]:
        rag = self.clients.rag
        corpus = self._corpus(tenant, collection)
        resp = rag.retrieval_query(
            rag_resources=[rag.RagResource(rag_corpus=corpus.name)],
            text=query,
            rag_retrieval_config=rag.RagRetrievalConfig(top_k=k),
        )
        known = self.docs.read(f"{self._prefix(tenant, collection)}/meta.json", {})
        hits = []
        for ctx in resp.contexts.contexts:
            uri = getattr(ctx, "source_uri", "") or ""
            doc_id = self.id_of(uri) if uri else str(getattr(ctx, "source_display_name", ""))
            raw = float(getattr(ctx, "score", 0.0) or 0.0)
            hits.append(
                Hit(
                    id=doc_id,
                    text=ctx.text,
                    score=self._score(raw),
                    metadata={
                        **dict(known.get(doc_id) or {}),
                        "source_uri": uri,
                        "raw_score": raw,
                        "score_kind": self.score_kind,
                    },
                )
            )
        return hits

    def count(self, tenant: Tenant, collection: str) -> int:
        corpus = self._corpus(tenant, collection)
        return sum(1 for _ in self.clients.rag.list_files(corpus_name=corpus.name))

    def drop(self, tenant: Tenant, collection: str) -> None:
        """Empties the corpus and deletes the source objects and the metadata. The corpus
        itself belongs to Terraform and stays."""
        rag = self.clients.rag
        corpus = self._corpus(tenant, collection)
        for f in list(rag.list_files(corpus_name=corpus.name)):
            rag.delete_file(name=f.name)
        self.docs.delete_prefix(f"{self._prefix(tenant, collection)}/")


# ----- AgentRuntime ---------------------------------------------------------------------------


class AgentEngineRuntime:
    """Agent Engine: the reasoning engine `northwind-<tenant>-agent`, which Terraform creates
    from the nw-agent image (a tenant has no permission to create one); `deploy` updates the
    image and environment of that engine in place and refuses when it is missing. `invoke` is
    `query` with the `route` class method of nw/agent/agentcore.py.

    The registry: a tenant writes its own card to `<prefix>/agents/<id>.json` (the only
    prefix it can write); `merge_registry` (`python -m nw.platform.gcp agents-merge`, run by the
    instructor or the live identity) folds every tenant's cards into `agents/agents.json`, which
    every tenant can read."""

    REGISTRY_PATH = "agents/agents.json"

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

    def card_path(self, tenant: Tenant, card_id: str) -> str:
        return f"{tenant.prefix}/agents/{card_id}.json"

    @property
    def _parent(self) -> str:
        return f"projects/{self.cfg.project}/locations/{self.cfg.region}"

    def _find(self, tenant: Tenant) -> Any | None:
        display = tenant.resource("agent")
        for engine in self.clients.reasoning_engines.list_reasoning_engines(parent=self._parent):
            if engine.display_name == display:
                return engine
        return None

    def deploy(self, tenant: Tenant, image: str, env: Mapping[str, str], *, version: str) -> str:
        from google.cloud import aiplatform_v1 as v1  # lazily: the message types

        engine = self._find(tenant)
        merged = dict(env)
        merged["NW_AGENT_VERSION"] = version
        merged.setdefault("NW_TENANT", tenant.name)
        merged.setdefault("NW_ENVIRONMENT", tenant.environment)
        if engine is None:
            raise KeyError(
                f"no reasoning engine {tenant.resource('agent')}: Terraform creates each "
                "tenant's engine (`make deploy-gcp`); a tenant can only update its own"
            )
        # update in place: the only change a tenant may make to its engine
        engine.spec.container_spec.image_uri = image
        existing = {e.name: e.value for e in engine.spec.deployment_spec.env}
        existing.update({k: str(x) for k, x in merged.items()})
        engine.spec.deployment_spec.env = [
            v1.EnvVar(name=k, value=x) for k, x in sorted(existing.items())
        ]
        op = self.clients.reasoning_engines.update_reasoning_engine(
            reasoning_engine=engine,
            update_mask={"paths": ["spec.container_spec.image_uri", "spec.deployment_spec.env"]},
        )
        engine = op.result()
        self._upsert_registry(
            tenant,
            {
                "id": tenant.resource("agent"),
                "resource": engine.name,
                "image": image,
                "agent_version": version,
                "status": "deployed",
                "deployed": _now(),
            },
        )
        return engine.name

    def invoke(
        self, tenant: Tenant, payload: Mapping[str, Any], *, session_id: str | None = None
    ) -> Mapping[str, Any]:
        engine = self._find(tenant)
        if engine is None:
            raise KeyError(f"no reasoning engine {tenant.resource('agent')}")
        body = dict(payload)
        if session_id:
            body["session_id"] = session_id
        resp = self.clients.reasoning_engine_execution.query_reasoning_engine(
            request={"name": engine.name, "class_method": "route", "input": body}
        )
        output = getattr(resp, "output", resp)
        return _to_plain(output)

    def register(self, tenant: Tenant, card: Mapping[str, Any]) -> str:
        entry = {
            "id": card.get("id") or tenant.resource(card.get("name", "agent")),
            "tenant": tenant.name,
            "runtime": "agent-engine",
            "registered": _now(),
            **{k: v for k, v in card.items() if k != "id"},
        }
        return self._upsert_registry(tenant, entry)

    def status(self, tenant: Tenant) -> Mapping[str, Any]:
        engine = self._find(tenant)
        if engine is None:
            return {"name": tenant.resource("agent"), "state": "absent"}
        env = {e.name: e.value for e in engine.spec.deployment_spec.env}
        card = self.docs.read(self.card_path(tenant, tenant.resource("agent")))
        return {
            "name": engine.name,
            "display_name": engine.display_name,
            "state": "deployed",
            "image": engine.spec.container_spec.image_uri,
            "agent_version": env.get("NW_AGENT_VERSION", ""),
            "update_time": str(getattr(engine, "update_time", "")),
            "registry": card,
        }

    def _upsert_registry(self, tenant: Tenant, entry: Mapping[str, Any]) -> str:
        """The tenant's own card under its prefix, merged into what it wrote before."""
        entry = {**entry, "tenant": tenant.name}
        path = self.card_path(tenant, str(entry["id"]))
        self.docs.update(path, lambda previous: {**previous, **entry}, {})
        return f"gs://{self.cfg.artifacts_bucket}/{path}"

    def merge_registry(self) -> str:
        """Every tenant's cards into `agents/agents.json` (the platform identity runs this)."""
        bucket = self.clients.storage.bucket(self.cfg.artifacts_bucket)
        cards = []
        for blob in bucket.list_blobs(prefix=f"{self.cfg.environment}-"):
            parts = blob.name.split("/")
            if len(parts) == 3 and parts[1] == "agents" and parts[2].endswith(".json"):
                cards.append(json.loads(blob.download_as_text()))

        def merge(registry: dict[str, Any]) -> dict[str, Any]:
            registry["agents"] = sorted(
                cards, key=lambda a: (str(a.get("tenant", "")), str(a.get("id", "")))
            )
            registry["updated"] = _now()
            return registry

        self.docs.update(self.REGISTRY_PATH, merge, {"environment": self.cfg.environment})
        return f"gs://{self.cfg.artifacts_bucket}/{self.REGISTRY_PATH}"


def _to_plain(value: Any) -> Any:
    """Protobuf Struct or Value to plain Python."""
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(v) for v in value]
    if hasattr(value, "items") and callable(value.items):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


# ----- factory --------------------------------------------------------------------------------


@dataclass
class GcpPlatform(Platform):
    """`Platform` plus the config, for scripts that need the bucket or region."""

    cfg: GcpConfig = field(default=None)  # type: ignore[assignment]


def build(settings: Settings, clients: GcpClients | None = None) -> GcpPlatform:
    cfg = GcpConfig.from_settings(settings)
    clients = clients or GcpClients(cfg)
    return GcpPlatform(
        track=Track.GCP,
        registry=VertexModelRegistry(cfg, clients),
        pipelines=VertexPipelineRunner(cfg, clients),
        endpoints=CloudRunEndpointClient(cfg, clients),
        prompts=VertexPromptStore(cfg, clients),
        vectors=RagEngineVectorStore(cfg, clients),
        agents=AgentEngineRuntime(cfg, clients),
        gateway_url=cfg.gateway_url,
        cfg=cfg,
    )


# ----- CLI --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`pipeline-upload [--tenants alice,bob] [name ...]`: upload the compiled pipelines to
    each tenant's `<prefix>/pipelines/`, where the Cloud Scheduler job reads them.
    `bootstrap [triage|semantic ...] [--force]`: register and approve `artifacts/*/latest`
    (`nw.platform.bootstrap`), the mid-course recovery. `agents-merge`: fold the tenants' agent
    cards into `agents/agents.json` (the instructor or the live identity runs it)."""
    import argparse
    import sys

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["agents-merge"]:
        from nw.config import settings as load_settings

        print(build(load_settings()).agents.merge_registry())  # type: ignore[attr-defined]
        return 0
    if raw[:1] == ["bootstrap"]:
        from nw.platform.bootstrap import main as bootstrap_main

        return bootstrap_main(raw[1:], track="gcp")

    from nw.config import settings as load_settings
    from nw.pipelines import NAMES, PIPELINES
    from nw.platform.base import tenant_from_env

    ap = argparse.ArgumentParser(prog="python -m nw.platform.gcp")
    argv = raw
    sub = ap.add_subparsers(dest="command", required=True)
    up = sub.add_parser("pipeline-upload", help="upload compiled pipelines for the tenants")
    up.add_argument("--tenants", default="", help="comma separated; default NW_TENANT or solo")
    up.add_argument("names", nargs="*", help=f"default: {' '.join(PIPELINES)} and their aliases")
    args = ap.parse_args(argv)
    names = args.names or list(PIPELINES)
    unknown = sorted(set(names) - set(NAMES))
    if unknown:
        ap.error(f"unknown pipeline {', '.join(unknown)}; expected {', '.join(NAMES)}")

    cfg = load_settings()
    runner = build(cfg).pipelines
    default = tenant_from_env(cfg)
    handles = [t for t in args.tenants.split(",") if t] or [default.name]
    for handle in handles:
        tenant = Tenant(name=handle, environment=default.environment)
        for name in names:
            print(runner.upload(tenant, name))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
