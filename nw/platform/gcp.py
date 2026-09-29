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
- the stage audit trail of model versions (aliases move, the reason is logged here)
- the agent registry document `agents/agents.json`
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nw.config import Settings, Track
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


class Documents:
    """JSON documents in the artifacts bucket, the store for everything the platform has no
    resource for. Thin on purpose so a fake needs three methods."""

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

    def write(self, path: str, doc: Any) -> str:
        self._blob(path).upload_from_string(
            json.dumps(doc, indent=2, sort_keys=True), content_type="application/json"
        )
        return f"gs://{self.bucket}/{path}"

    def append(self, path: str, row: Mapping[str, Any]) -> None:
        blob = self._blob(path)
        text = blob.download_as_text() if blob.exists() else ""
        blob.upload_from_string(text + json.dumps(row, sort_keys=True) + "\n")

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


def _labels(tenant: Tenant, tags: Mapping[str, str]) -> dict[str, str]:
    """Vertex labels: lowercase letters, digits, underscore and dash, 63 characters."""
    out = {"tenant": tenant.name, "environment": tenant.environment}
    for k, v in tags.items():
        key = re.sub(r"[^a-z0-9_-]", "_", k.lower())[:63]
        out[key] = re.sub(r"[^a-z0-9_-]", "_", str(v).lower())[:63]
    return out


class VertexModelRegistry:
    """Model Registry: one model per tenant and name, one version per registration, the
    stage as a version alias (`candidate`, `approved`, `live`, `retired`). Aliases are unique
    per model, so adding `live` to a version takes it from the previous one, which is the
    promotion. Metrics travel in the version description; the reason of every stage change
    is appended to `<prefix>/registry/<name>/stages.jsonl`."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

    def _parent(self, tenant: Tenant, name: str) -> Any | None:
        display = tenant.resource(name)
        found = self.clients.aiplatform.Model.list(filter=f'display_name="{display}"')
        return found[0] if found else None

    def _to_version(self, name: str, model: Any, version: Any | None = None) -> ModelVersion:
        v = version if version is not None else model
        aliases = list(getattr(v, "version_aliases", None) or [])
        desc = getattr(v, "version_description", None) or ""
        metrics: dict[str, float] = {}
        try:
            metrics = {k: float(x) for k, x in json.loads(desc).get("metrics", {}).items()}
        except (ValueError, AttributeError):
            pass
        return ModelVersion(
            name=name,
            version=str(getattr(v, "version_id", "")),
            stage=_stage_from_aliases(aliases),
            uri=getattr(model, "uri", None) or getattr(v, "uri", "") or "",
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
            version_aliases=[Stage.CANDIDATE.value],
            version_description=json.dumps({"metrics": dict(metrics), "registered": _now()}),
            labels=_labels(tenant, tags),
        )
        out = self._to_version(name, model)
        out.uri = uri
        out.metrics = dict(metrics)
        self.docs.append(
            f"{tenant.prefix}/registry/{name}/stages.jsonl",
            {
                "version": out.version,
                "stage": Stage.CANDIDATE.value,
                "reason": "registered",
                "at": _now(),
            },
        )
        return out

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        parent = self._parent(tenant, name)
        if parent is None:
            raise KeyError(f"no model {tenant.resource(name)} in the registry")
        registry = parent.versioning_registry
        current = [
            v for v in registry.list_versions() if str(getattr(v, "version_id", "")) == version
        ]
        if not current:
            raise KeyError(f"{tenant.resource(name)} has no version {version}")
        old = [a for a in (current[0].version_aliases or []) if a in STAGE_ALIASES]
        if old:
            registry.remove_version_aliases(version_name=version, alias_names=old)
        registry.add_version_aliases(version_name=version, alias_names=[stage.value])
        self.docs.append(
            f"{tenant.prefix}/registry/{name}/stages.jsonl",
            {"version": version, "stage": stage.value, "reason": reason, "at": _now()},
        )
        refreshed = [
            v for v in registry.list_versions() if str(getattr(v, "version_id", "")) == version
        ]
        out = self._to_version(name, parent, refreshed[0] if refreshed else current[0])
        out.stage = stage
        return out

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        parent = self._parent(tenant, name)
        if parent is None:
            return []
        return [
            self._to_version(name, parent, v) for v in parent.versioning_registry.list_versions()
        ]

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        for v in self.versions(tenant, name):
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
    the last hand submission use one definition. `upload` alone is `make pipeline-upload-gcp`."""

    def __init__(
        self, cfg: GcpConfig, clients: GcpClients, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.sleep = sleep

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

    def upload(self, tenant: Tenant, pipeline: str) -> str:
        """Copy `<NW_PIPELINE_DIR>/<pipeline>.yaml` to `template_uri`, and for `triage` or
        `semantic` to the scheduler's name too (`retrain-triage.yaml`); returns the URI of
        `pipeline`."""
        from nw.pipelines import ALIASES, aliases_of

        local = self._local(pipeline)
        names = [pipeline] + ([] if pipeline in ALIASES else aliases_of(pipeline))
        for name in names:
            bucket, blob = split_gcs(self.template_uri(tenant, name))
            self.clients.storage.bucket(bucket).blob(blob).upload_from_filename(str(local))
        return self.template_uri(tenant, pipeline)

    def _template(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> str:
        explicit = params.get("template_path")
        if explicit:
            return str(explicit)
        return self.upload(tenant, pipeline)

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
        job = self.clients.aiplatform.PipelineJob(
            display_name=tenant.resource(pipeline),
            template_path=self._template(tenant, pipeline, params),
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
    split. `deploy(live=False)` rewrites the service's environment (a new revision),
    `deploy(live=True, canary_percent=N)` deploys the version on the live endpoint at N percent
    (100 when 0) and leaves the rest on what was serving. The live Cloud Run services move
    through Cloud Deploy (`make release-gcp`), not through this client."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients, api_key: str | None = None) -> None:
        self.cfg = cfg
        self.clients = clients
        self.api_key = api_key or os.environ.get("NW_API_KEY")

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
            endpoint = self.clients.aiplatform.Endpoint(self.cfg.live_endpoint_name(version.name))
            model = self._model_resource(tenant, version)
            model.deploy(
                endpoint=endpoint,
                deployed_model_display_name=f"{tenant.resource(version.name)}-v{version.version}",
                traffic_percentage=canary_percent or 100,
                machine_type="n1-standard-2",
                min_replica_count=1,
                max_replica_count=1,
            )
            return endpoint.resource_name
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
            return {
                "kind": "vertex-endpoint",
                "name": endpoint.resource_name,
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
    stage and tags, and `index.json` with the stage map; the version id the course uses is the
    twelve-character SHA-256 of the text, the same `prompt_version` that travels in answers.
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
            for stage in STAGE_PRIORITY:
                hits = [v for v, s in stages.items() if s == stage.value]
                if hits and stage != Stage.RETIRED:
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
        index = self.docs.read(f"{directory}/index.json", {"prompt_id": None, "versions": {}})
        if stage in (Stage.LIVE, Stage.APPROVED):
            # One version holds a stage at a time; the previous holder steps back.
            for v, s in list(index["versions"].items()):
                if s == stage.value and v != version:
                    index["versions"][v] = (
                        Stage.RETIRED.value if stage == Stage.LIVE else Stage.CANDIDATE.value
                    )
                    other = self.docs.read(f"{directory}/{v}.json")
                    if other:
                        other["stage"] = index["versions"][v]
                        self.docs.write(f"{directory}/{v}.json", other)
        doc["stage"] = stage.value
        index["versions"][version] = stage.value
        self.docs.write(f"{directory}/{version}.json", doc)
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


# ----- VectorStore ---------------------------------------------------------------------------


class RagEngineVectorStore:
    """RAG Engine: the corpus `northwind-<tenant>-<collection>` (Terraform creates
    `policies`), files imported from the artifacts bucket, retrieval through
    `retrieval_query`. RAG Engine chunks and embeds on import, so `vectors` is ignored and
    the caller's ids become file names; a hit's id is the file's display name."""

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

    def _corpus(self, tenant: Tenant, collection: str, create: bool = False) -> Any:
        rag = self.clients.rag
        display = tenant.resource(collection)
        for corpus in rag.list_corpora():
            if corpus.display_name == display:
                return corpus
        if not create:
            raise KeyError(f"no RAG corpus {display}; `make deploy-gcp` creates it")
        return rag.create_corpus(display_name=display)

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int:
        corpus = self._corpus(tenant, collection, create=True)
        prefix = f"{tenant.prefix}/rag/{collection}"
        for i, (doc_id, text) in enumerate(zip(ids, texts, strict=True)):
            meta = dict(metadata[i]) if i < len(metadata) else {}
            self.docs.upload_text(f"{prefix}/{doc_id}.txt", text)
            self.docs.write(f"{prefix}/{doc_id}.meta.json", {"id": doc_id, **meta})
        rag = self.clients.rag
        rag.import_files(
            corpus_name=corpus.name,
            paths=[f"gs://{self.cfg.artifacts_bucket}/{prefix}/"],
            transformation_config=rag.TransformationConfig(
                chunking_config=rag.ChunkingConfig(chunk_size=512, chunk_overlap=64)
            ),
            max_embedding_requests_per_min=600,
        )
        return len(ids)

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
        hits = []
        for ctx in resp.contexts.contexts:
            uri = getattr(ctx, "source_uri", "") or ""
            doc_id = uri.rsplit("/", 1)[-1].removesuffix(".txt") or getattr(
                ctx, "source_display_name", ""
            )
            hits.append(
                Hit(
                    id=doc_id,
                    text=ctx.text,
                    score=float(getattr(ctx, "score", 0.0) or 0.0),
                    metadata={"source_uri": uri},
                )
            )
        return hits

    def count(self, tenant: Tenant, collection: str) -> int:
        corpus = self._corpus(tenant, collection)
        return sum(1 for _ in self.clients.rag.list_files(corpus_name=corpus.name))

    def drop(self, tenant: Tenant, collection: str) -> None:
        """Empties the corpus. The corpus itself belongs to Terraform and stays."""
        rag = self.clients.rag
        corpus = self._corpus(tenant, collection)
        for f in list(rag.list_files(corpus_name=corpus.name)):
            rag.delete_file(name=f.name)


# ----- AgentRuntime ---------------------------------------------------------------------------


class AgentEngineRuntime:
    """Agent Engine: the reasoning engine `northwind-<tenant>-agent` (Terraform creates it
    from the nw-agent image; `deploy` updates image and environment in place), `query` with
    the `route` class method of nw/agent/agentcore.py, and the registry document
    `agents/agents.json` in the artifacts bucket."""

    REGISTRY_PATH = "agents/agents.json"

    def __init__(self, cfg: GcpConfig, clients: GcpClients) -> None:
        self.cfg = cfg
        self.clients = clients
        self.docs = Documents(clients, cfg.artifacts_bucket)

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
            spec = v1.ReasoningEngineSpec(
                agent_framework="custom",
                service_account=f"nw-{tenant.name}-agent@{self.cfg.project}.iam.gserviceaccount.com",
                container_spec=v1.ReasoningEngineSpec.ContainerSpec(image_uri=image, port=8000),
                deployment_spec=v1.ReasoningEngineSpec.DeploymentSpec(
                    env=[v1.EnvVar(name=k, value=str(x)) for k, x in sorted(merged.items())]
                ),
            )
            op = self.clients.reasoning_engines.create_reasoning_engine(
                parent=self._parent,
                reasoning_engine=v1.ReasoningEngine(
                    display_name=tenant.resource("agent"),
                    description=f"Northwind resolver of tenant {tenant.name}",
                    spec=spec,
                ),
            )
            engine = op.result()
        else:
            engine.spec.container_spec.image_uri = image
            existing = {e.name: e.value for e in engine.spec.deployment_spec.env}
            existing.update({k: str(x) for k, x in merged.items()})
            engine.spec.deployment_spec.env = [
                v1.EnvVar(name=k, value=x) for k, x in sorted(existing.items())
            ]
            op = self.clients.reasoning_engines.update_reasoning_engine(
                reasoning_engine=engine,
                update_mask={
                    "paths": ["spec.container_spec.image_uri", "spec.deployment_spec.env"]
                },
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
        registry = self.docs.read(self.REGISTRY_PATH, {"agents": []})
        card = next((a for a in registry.get("agents", []) if a.get("tenant") == tenant.name), None)
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
        entry = {**entry, "tenant": tenant.name}
        registry = self.docs.read(
            self.REGISTRY_PATH, {"environment": tenant.environment, "agents": []}
        )
        agents = [
            a
            for a in registry.get("agents", [])
            if not (a.get("tenant") == tenant.name and a.get("id") == entry["id"])
        ]
        previous = next(
            (
                a
                for a in registry.get("agents", [])
                if a.get("tenant") == tenant.name and a.get("id") == entry["id"]
            ),
            {},
        )
        agents.append({**previous, **entry})
        registry["agents"] = sorted(agents, key=lambda a: (a.get("tenant", ""), a.get("id", "")))
        registry["updated"] = _now()
        return self.docs.write(self.REGISTRY_PATH, registry)


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
    each tenant's `<prefix>/pipelines/`, where the Cloud Scheduler job reads them."""
    import argparse

    from nw.config import settings as load_settings
    from nw.pipelines import NAMES, PIPELINES
    from nw.platform.base import tenant_from_env

    ap = argparse.ArgumentParser(prog="python -m nw.platform.gcp")
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
