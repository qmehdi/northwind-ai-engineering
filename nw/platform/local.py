"""The Local track's platform: every protocol of `nw.platform.base` on the compose stack
(ADR 0011). One open-source service per managed box:

| Protocol       | Local service                 | AWS                     | Google Cloud          |
| -------------- | ----------------------------- | ----------------------- | --------------------- |
| ModelRegistry  | MLflow registry, aliases      | SageMaker Model Registry| Vertex Model Registry |
| PipelineRunner | Kubeflow Pipelines SDK, local | SageMaker Pipelines     | Vertex AI Pipelines   |
| EndpointClient | MLflow serving, nginx weights | Endpoint and CodeDeploy | Endpoint traffic split|
| PromptStore    | MLflow prompt registry        | Bedrock Prompt Mgmt     | Gen AI SDK prompts    |
| VectorStore    | Qdrant                        | Knowledge Base, S3 Vec. | RAG Engine            |
| AgentRuntime   | the agent container           | AgentCore Runtime       | Agent Engine          |

Everything here is reachable from the laptop through published ports; the defaults match
`docker-compose.yml` and every one can be moved with an environment variable (see
`LocalConfig`). SDKs are imported inside the methods that need them, so importing this module
costs nothing and a test can pick MLflow on a temporary sqlite file or Qdrant in memory.

    uv run python -m nw.platform.local status              # what is live, the canary weights
    uv run python -m nw.platform.local canary 10           # ten percent to the canary replica
    uv run python -m nw.platform.local promote             # the live version into `higher`
    uv run python -m nw.platform.local bootstrap           # register artifacts/triage/latest
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nw.config import Settings, Track
from nw.llm.prompts import prompt_hash
from nw.logging import get_logger
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

log = get_logger("nw.platform.local")

STAGE_ORDER = (Stage.LIVE, Stage.APPROVED, Stage.CANDIDATE, Stage.RETIRED)
CANARY_ALIAS = "canary"
HIGHER_ENVIRONMENT = "higher"
_QDRANT_NS = uuid.UUID("6f1f5c2e-9a7c-4f6e-8a2b-1f0c3d4e5a6b")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- configuration -----------------------------------------------------------------


@dataclass(frozen=True)
class LocalConfig:
    """Where every box of the stack answers, from the environment with compose defaults."""

    mlflow_uri: str = "http://localhost:5001"
    qdrant_url: str = "http://localhost:6333"
    endpoint_url: str = "http://localhost:8005"
    agent_url: str = "http://localhost:8014"
    gateway_url: str | None = None
    pipeline_root: Path = Path("artifacts/pipelines")
    kfp_runner: str = "subprocess"
    proxy_dir: Path = Path("deploy/local/proxy")
    registry_dir: Path = Path("deploy/local/registry")
    project_dir: Path = Path(".")
    api_key: str | None = None
    embedder: str = "hash"

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> LocalConfig:
        env = os.environ
        gateway = getattr(settings, "gateway_url", None) or env.get("NW_GATEWAY_URL") or None
        api_key = getattr(settings, "api_key", None) or env.get("NW_API_KEY") or None
        return cls(
            mlflow_uri=env.get("NW_MLFLOW_URI", cls.mlflow_uri),
            qdrant_url=env.get("NW_QDRANT_URL", cls.qdrant_url),
            endpoint_url=env.get("NW_ENDPOINT_URL", cls.endpoint_url),
            agent_url=env.get("NW_AGENT_RUNTIME_URL", cls.agent_url),
            gateway_url=gateway,
            pipeline_root=Path(env.get("NW_PIPELINE_ROOT", str(cls.pipeline_root))),
            kfp_runner=env.get("NW_KFP_RUNNER", cls.kfp_runner),
            proxy_dir=Path(env.get("NW_PROXY_DIR", str(cls.proxy_dir))),
            registry_dir=Path(env.get("NW_LOCAL_REGISTRY_DIR", str(cls.registry_dir))),
            project_dir=Path(env.get("NW_COMPOSE_DIR", ".")),
            api_key=api_key,
            embedder=env.get("NW_VECTOR_EMBEDDER", cls.embedder),
        )


class ComposeRunner:
    """`docker compose` for the few operations the platform performs on its own containers:
    restart a serving replica after an alias moved, reload the proxy, recreate the agent."""

    def __init__(self, project_dir: Path, profiles: Sequence[str] = ("platform",)) -> None:
        self.project_dir = project_dir
        self.profiles = tuple(profiles)

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        profiles = list(self.profiles)
        if any(a.endswith("-higher") for a in args) and HIGHER_ENVIRONMENT not in profiles:
            profiles.append(HIGHER_ENVIRONMENT)
        cmd = ["docker", "compose"]
        for p in profiles:
            cmd += ["--profile", p]
        cmd += list(args)
        merged = {**os.environ, **(env or {})}
        log.info("compose", extra={"argv": " ".join(args)})
        return subprocess.run(
            cmd, cwd=self.project_dir, env=merged, check=False, capture_output=True, text=True
        )


class NoopCompose(ComposeRunner):
    """For tests and for callers that only touch the registry: records, never runs."""

    def __init__(self) -> None:
        super().__init__(Path("."))
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")


# ----- MLflow helpers ------------------------------------------------------------------


def _proxied_downloads() -> None:
    """Download artifacts through the MLflow server, never from presigned object store URLs.

    MLflow 3.16 advertises presigned downloads when its artifact store is S3; the URLs it signs
    name `rustfs:9000`, which only resolves on the compose network, so a laptop client retried
    them forever. The stack's object store is private by design: every download goes through
    the tracking server's proxy. An explicit setting in the environment still wins.
    """
    os.environ.setdefault("MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD", "false")


def _mlflow(uri: str) -> Any:
    _proxied_downloads()
    import mlflow

    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    return mlflow


def _client(uri: str) -> Any:
    _proxied_downloads()
    from mlflow import MlflowClient

    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def _stage_of(aliases: Sequence[str], tag: str | None) -> Stage:
    """The stage of a version (the rules of `nw.platform.base`): the `live` or `approved`
    alias when it holds one, since an alias has one holder per model; else its `stage` tag,
    where a `live` or `approved` tag whose alias moved to a newer holder reads as stepped back
    (live to retired, approved to candidate); else the `candidate` or `retired` alias."""
    for stage in UNIQUE_STAGES:
        if stage.value in aliases:
            return stage
    if tag in {s.value for s in Stage}:
        stage = Stage(tag)  # type: ignore[arg-type]
        return STEP_BACK.get(stage, stage)
    for stage in STAGE_ORDER:
        if stage.value in aliases:
            return stage
    return Stage.CANDIDATE


# ----- ModelRegistry -----------------------------------------------------------------


class LocalModelRegistry:
    """MLflow's registry with one registered model per tenant and artifact, aliases for stages.

    A version is a pyfunc (`nw.platform.local_pyfunc.ArtifactModel`) over the artifact
    directory, logged to the tenant's experiment with the metrics and tags given, registered
    as `<environment>-<tenant>-<name>` with the alias `candidate`. `set_stage` moves the alias
    of the stage (an alias is unique per model, so the previous holder loses it) and records
    the reason on the version. Artifacts go through the tracking server's artifact proxy to
    the object store, so no caller needs object-store credentials."""

    def __init__(self, uri: str) -> None:
        self.uri = uri

    def model_name(self, tenant: Tenant, name: str) -> str:
        return tenant.resource(name)

    def _experiment(self, mlflow: Any, tenant: Tenant) -> str:
        exp = mlflow.get_experiment_by_name(tenant.prefix)
        return exp.experiment_id if exp else mlflow.create_experiment(tenant.prefix)

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
    ) -> ModelVersion:
        from nw.platform.local_pyfunc import ArtifactModel, detect_kind

        artifact = Path(artifact).resolve()
        if not artifact.is_dir():
            raise FileNotFoundError(f"{artifact} is not an artifact directory")
        mlflow = _mlflow(self.uri)
        client = _client(self.uri)
        model_name = self.model_name(tenant, name)
        version_tags = {
            "kind": detect_kind(artifact),
            "tenant": tenant.name,
            "environment": tenant.environment,
            "stage": Stage.CANDIDATE.value,
            "registered_at": _now(),
            **{k: str(v) for k, v in tags.items()},
        }
        meta = artifact / "metadata.json"
        if meta.exists():
            try:
                version_tags.setdefault(
                    "artifact_version", str(json.loads(meta.read_text())["version"])
                )
            except (KeyError, ValueError):
                pass
        exp_id = self._experiment(mlflow, tenant)
        with mlflow.start_run(experiment_id=exp_id, run_name=f"{name}-{_now()}"):
            if metrics:
                mlflow.log_metrics({k: float(v) for k, v in metrics.items()})
            mlflow.set_tags(version_tags)
            info = mlflow.pyfunc.log_model(
                name="model",
                python_model=ArtifactModel(),
                artifacts={"artifact": str(artifact)},
                registered_model_name=model_name,
                pip_requirements=["mlflow", "scikit-learn", "joblib", "numpy"],
            )
        version = str(info.registered_model_version)
        for k, v in version_tags.items():
            client.set_model_version_tag(model_name, version, k, v)
        client.set_registered_model_alias(model_name, Stage.CANDIDATE.value, version)
        client.set_registered_model_tag(model_name, "tenant", tenant.name)
        client.set_registered_model_tag(model_name, "environment", tenant.environment)
        log.info("registered", extra={"model": model_name, "version": version})
        return ModelVersion(
            name=name,
            version=version,
            stage=Stage.CANDIDATE,
            uri=f"models:/{model_name}/{version}",
            metrics=dict(metrics),
            tags=version_tags,
        )

    def _to_version(self, name: str, mv: Any) -> ModelVersion:
        tags = dict(mv.tags or {})
        metrics: dict[str, float] = {}
        if mv.run_id:
            try:
                metrics = dict(_client(self.uri).get_run(mv.run_id).data.metrics)
            except Exception:  # noqa: BLE001  a run deleted under the version
                metrics = {}
        return ModelVersion(
            name=name,
            version=str(mv.version),
            stage=_stage_of(list(mv.aliases or []), tags.get("stage")),
            uri=f"models:/{mv.name}/{mv.version}",
            metrics=metrics,
            tags=tags,
        )

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        client = _client(self.uri)
        model_name = self.model_name(tenant, name)
        mv = client.get_model_version(model_name, version)
        if stage in UNIQUE_STAGES:
            previous = self.by_alias(tenant, name, stage.value)
            if previous is not None and previous.version != str(version):
                back = STEP_BACK[stage].value
                client.set_model_version_tag(model_name, previous.version, "stage", back)
                client.set_model_version_tag(
                    model_name, previous.version, "stage_reason", f"replaced by {version}"
                )
        for old in mv.aliases or []:
            if old in {s.value for s in Stage} and old != stage.value:
                client.delete_registered_model_alias(model_name, old)
        client.set_registered_model_alias(model_name, stage.value, version)
        client.set_model_version_tag(model_name, version, "stage", stage.value)
        client.set_model_version_tag(model_name, version, "stage_reason", reason)
        client.set_model_version_tag(model_name, version, "stage_at", _now())
        log.info("stage", extra={"model": model_name, "version": version, "stage": stage.value})
        return self._to_version(name, client.get_model_version(model_name, version))

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        client = _client(self.uri)
        model_name = self.model_name(tenant, name)
        # search results carry no aliases; the per-version read does
        found = [
            client.get_model_version(model_name, mv.version)
            for mv in client.search_model_versions(f"name='{model_name}'")
        ]
        return sorted((self._to_version(name, mv) for mv in found), key=lambda v: int(v.version))

    def by_alias(self, tenant: Tenant, name: str, alias: str) -> ModelVersion | None:
        client = _client(self.uri)
        try:
            mv = client.get_model_version_by_alias(self.model_name(tenant, name), alias)
        except Exception:  # noqa: BLE001  no such model or alias
            return None
        return self._to_version(name, mv)

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        return self.by_alias(tenant, name, Stage.LIVE.value)

    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path:
        _mlflow(self.uri)
        from mlflow.artifacts import download_artifacts

        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        root = Path(download_artifacts(artifact_uri=version.uri, dst_path=str(into)))
        # the pyfunc keeps the artifact directory under artifacts/<its own name>
        holder = root / "artifacts"
        inner = [p for p in holder.glob("*") if p.is_dir()] if holder.is_dir() else []
        return inner[0] if len(inner) == 1 else root

    def copy_to(
        self, tenant: Tenant, name: str, version: ModelVersion, target: Tenant
    ) -> ModelVersion:
        """The promotion into a higher environment: the same version, copied (not retrained)
        under the target environment's name, with its lineage tags."""
        client = _client(self.uri)
        dst = self.model_name(target, name)
        try:
            client.create_registered_model(dst)
        except Exception:  # noqa: BLE001  exists
            pass
        copied = client.copy_model_version(version.uri, dst)
        for k, v in dict(version.tags).items():
            client.set_model_version_tag(dst, copied.version, k, v)
        client.set_model_version_tag(dst, copied.version, "promoted_from", version.uri)
        client.set_model_version_tag(dst, copied.version, "environment", target.environment)
        # the copy starts over as a candidate in its environment; its source stage is lineage
        client.set_model_version_tag(dst, copied.version, "stage", Stage.CANDIDATE.value)
        client.set_model_version_tag(
            dst, copied.version, "source_stage", str(dict(version.tags).get("stage", ""))
        )
        client.set_registered_model_alias(dst, Stage.CANDIDATE.value, copied.version)
        return self._to_version(name, client.get_model_version(dst, copied.version))


# ----- PipelineRunner ----------------------------------------------------------------


def pipeline_dir() -> Path:
    """Where compiled pipelines live: `NW_PIPELINE_DIR`, else `artifacts/pipelines`."""
    from nw.pipelines import COMPILED_DIR

    return Path(os.environ.get("NW_PIPELINE_DIR") or COMPILED_DIR)


def resolve_pipeline(name: str) -> Path:
    """A pipeline name (`triage`, `semantic`, `retrain-triage`, `retrain-semantic`) to
    `<NW_PIPELINE_DIR>/<name>.yaml`, compiling every pipeline first when the file is missing,
    the way `make pipeline-compile` would (`NW_PIPELINE_IMAGE` names the image)."""
    from nw.pipelines import DEFAULT_IMAGE

    path = pipeline_dir() / f"{name}.yaml"
    if not path.exists():
        from nw.pipelines.kfp import pipelines as kfp_pipelines

        kfp_pipelines.compile_all(path.parent, os.environ.get("NW_PIPELINE_IMAGE") or DEFAULT_IMAGE)
    if not path.exists():
        raise FileNotFoundError(f"compiling the pipelines did not write {path}")
    return path


def load_pipeline(pipeline: str | Callable[..., Any]) -> tuple[str, Callable[..., Any]]:
    """A pipeline name (`triage`, resolved by `resolve_pipeline`), a compiled pipeline
    (`path/to/pipeline.yaml`), a dotted callable (`nw.pipelines.triage:pipeline`) or the
    callable itself; returns its name and something to call with the parameters."""
    from nw.pipelines import NAMES

    if callable(pipeline):
        return getattr(pipeline, "name", None) or pipeline.__name__, pipeline
    if pipeline in NAMES:
        pipeline = str(resolve_pipeline(pipeline))
    path = Path(pipeline)
    if path.suffix in {".yaml", ".yml", ".json"}:
        if not path.exists():
            raise FileNotFoundError(path)
        from kfp import components

        comp = components.load_component_from_file(str(path))
        return comp.name, comp
    if ":" not in pipeline:
        raise ValueError(f"pipeline {pipeline!r}: expected a compiled .yaml or module:callable")
    module, attr = pipeline.split(":", 1)
    return attr, getattr(importlib.import_module(module), attr)


class LocalPipelineRunner:
    """The Kubeflow Pipelines SDK's local runner, the same SDK the Google track compiles for
    Vertex AI Pipelines, so one definition serves two tracks. A run executes in its own child
    process (`python -m nw.platform.local _execute <run_dir>`) under
    `<pipeline_root>/<tenant>/<run_id>`: `run.json` is the record `status` reads, `logs.txt` is
    the child's output, which `logs` streams, and the runner's own task outputs sit beside them.

    The child owns everything process-wide the kfp runner needs (its stdout, the
    `sys.executable` it splices into shell commands), so a submit never swaps them in the caller,
    and the run outlives a CLI that submits and exits. A pipeline given as a Python callable
    cannot cross a process boundary and runs in a thread of this process instead, writing its
    failure to `logs.txt` without touching `sys.stdout`."""

    runs_in_process = False

    def __init__(self, root: Path, runner: str = "subprocess") -> None:
        self.root = Path(root)
        self.runner = runner
        self._threads: dict[str, threading.Thread] = {}
        self._children: dict[str, subprocess.Popen] = {}

    def run_dir(self, tenant: Tenant, run_id: str) -> Path:
        return self.root / tenant.prefix / run_id

    @staticmethod
    def _write(run_dir: Path, record: Mapping[str, Any]) -> None:
        tmp = run_dir / "run.json.tmp"
        tmp.write_text(json.dumps(dict(record), indent=1, default=str))
        os.replace(tmp, run_dir / "run.json")

    @staticmethod
    def _read(run_dir: Path) -> dict[str, Any]:
        return json.loads((run_dir / "run.json").read_text())

    def submit(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> PipelineRun:
        ref = params.get("template_path") or pipeline
        name, func = load_pipeline(ref)
        params = {k: v for k, v in params.items() if k != "template_path"}
        run_id = f"{name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_dir = self.run_dir(tenant, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "pipeline": name,
            "run_id": run_id,
            "status": RunStatus.QUEUED.value,
            "params": dict(params),
            "outputs": {},
            "submitted_at": _now(),
            "runner": self.runner,
            "ref": None if callable(ref) else str(ref),
        }
        self._write(run_dir, record)
        if callable(ref):
            thread = threading.Thread(
                target=_execute_in_thread, args=(run_dir, func, record), name=run_id, daemon=True
            )
            self._threads[run_id] = thread
            thread.start()
        else:
            with (run_dir / "logs.txt").open("w", encoding="utf-8") as log_file:
                self._children[run_id] = subprocess.Popen(
                    [sys.executable, "-m", "nw.platform.local", "_execute", str(run_dir)],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=os.getcwd(),
                    start_new_session=True,
                )
        return PipelineRun(pipeline=name, run_id=run_id, status=RunStatus.RUNNING, url=str(run_dir))

    def status(self, tenant: Tenant, run: PipelineRun) -> PipelineRun:
        run_dir = self.run_dir(tenant, run.run_id)
        if not (run_dir / "run.json").exists():
            raise KeyError(f"no run {run.run_id} for {tenant.prefix}")
        record = self._read(run_dir)
        child = self._children.get(run.run_id)
        finished = (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.STOPPED.value)
        if child is not None and child.poll() is not None and record["status"] not in finished:
            # the child died before it could write its outcome
            record = {
                **record,
                "status": RunStatus.FAILED.value,
                "error": f"runner process exited with code {child.returncode}",
                "finished_at": _now(),
            }
            self._write(run_dir, record)
        return PipelineRun(
            pipeline=record["pipeline"],
            run_id=run.run_id,
            status=RunStatus(record["status"]),
            url=str(run_dir),
            outputs=dict(record.get("outputs", {})),
        )

    def wait(self, tenant: Tenant, run: PipelineRun, timeout_s: float = 1800) -> PipelineRun:
        deadline = time.monotonic() + timeout_s
        thread = self._threads.get(run.run_id)
        child = self._children.get(run.run_id)
        while True:
            current = self.status(tenant, run)
            if current.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED):
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"run {run.run_id} still {current.status.value} after {timeout_s}s"
                )
            if thread is not None:
                thread.join(timeout=0.2)
            elif child is not None:
                with _suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=0.5)
            else:
                time.sleep(0.5)

    def logs(self, tenant: Tenant, run: PipelineRun) -> Iterator[str]:
        path = self.run_dir(tenant, run.run_id) / "logs.txt"
        if not path.exists():
            return iter(())
        return iter(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


def _finish(run_dir: Path, record: Mapping[str, Any], **fields: Any) -> None:
    LocalPipelineRunner._write(run_dir, {**record, **fields, "finished_at": _now()})


def _execute_in_thread(run_dir: Path, func: Callable[..., Any], record: dict[str, Any]) -> None:
    """A callable pipeline, in this process: no kfp runner, no stream swaps."""
    LocalPipelineRunner._write(run_dir, {**record, "status": RunStatus.RUNNING.value})
    try:
        task = func(**dict(record["params"]))
        outputs = getattr(task, "outputs", None) or {}
        _finish(
            run_dir,
            record,
            status=RunStatus.SUCCEEDED.value,
            outputs={k: str(v) for k, v in outputs.items()},
        )
    except Exception as exc:  # noqa: BLE001  the record carries the failure
        with (run_dir / "logs.txt").open("a", encoding="utf-8") as fh:
            fh.write(f"pipeline failed: {exc}\n")
        _finish(run_dir, record, status=RunStatus.FAILED.value, error=str(exc))


def _space_free_python() -> str:
    """The SubprocessRunner splices `sys.executable` into an unquoted shell command, so an
    interpreter under a path with a space (this workspace) never starts. A one-line wrapper in
    the temp directory, which has no spaces, execs the real one."""
    real = sys.executable
    if " " not in real:
        return real
    wrapper_dir = Path(tempfile.gettempdir()) / "nw-kfp"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


def execute_run(run_dir: Path) -> int:
    """The child process of a local run: load the pipeline, run it on the kfp local runner,
    write the outcome into `run.json`. Its stdout is the run's `logs.txt`."""
    run_dir = Path(run_dir)
    record = LocalPipelineRunner._read(run_dir)
    LocalPipelineRunner._write(run_dir, {**record, "status": RunStatus.RUNNING.value})
    try:
        from kfp import local

        _, func = load_pipeline(record["ref"])
        sys.executable = _space_free_python()  # this process only
        runner = local.DockerRunner() if record.get("runner") == "docker" else None
        local.init(
            runner=runner or local.SubprocessRunner(use_venv=False),
            pipeline_root=str(run_dir),
            raise_on_error=True,
        )
        task = func(**dict(record["params"]))
        outputs = getattr(task, "outputs", None) or {}
        _finish(
            run_dir,
            record,
            status=RunStatus.SUCCEEDED.value,
            outputs={k: str(v) for k, v in outputs.items()},
        )
        return 0
    except Exception as exc:  # noqa: BLE001  the record carries the failure
        print(f"pipeline failed: {exc}", flush=True)
        _finish(run_dir, record, status=RunStatus.FAILED.value, error=str(exc))
        return 1


# ----- EndpointClient ----------------------------------------------------------------

_UPSTREAM_RE = re.compile(
    r"server\s+(?P<host>[\w.-]+):\d+\s+weight=(?P<weight>\d+)(?P<down>\s+down)?\s*;"
)


@dataclass(frozen=True)
class Weights:
    stable: int
    canary: int

    @property
    def canary_percent(self) -> int:
        return self.canary


def render_upstream(
    canary_percent: int, stable: str = "serving-stable", canary: str = "serving-canary"
) -> str:
    """The nginx upstream block for a canary share. nginx rejects weight=0, so a replica at
    zero percent is written with weight 1 and marked `down`, which nginx never routes to."""
    if not 0 <= int(canary_percent) <= 100:
        raise ValueError(f"canary percent must be 0 to 100, got {canary_percent}")
    c = int(canary_percent)
    s = 100 - c
    lines = [f"# generated by nw.platform.local: canary {c} percent", "upstream serving {"]
    lines.append(f"    server {stable}:8000 weight={s if s else 1}{'' if s else ' down'};")
    lines.append(f"    server {canary}:8000 weight={c if c else 1}{'' if c else ' down'};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def parse_upstream(text: str) -> Weights:
    """The weights back out of an upstream block (`down` reads as zero)."""
    found: dict[str, int] = {}
    for m in _UPSTREAM_RE.finditer(text):
        weight = 0 if m.group("down") else int(m.group("weight"))
        host = m.group("host")
        key = "canary" if "canary" in host else "stable"
        found[key] = weight
    if set(found) != {"stable", "canary"}:
        raise ValueError("upstream block needs one stable and one canary server")
    total = found["stable"] + found["canary"]
    if total == 0:
        raise ValueError("both replicas are down")
    return Weights(
        stable=round(100 * found["stable"] / total), canary=round(100 * found["canary"] / total)
    )


class LocalEndpointClient:
    """MLflow model serving behind an nginx proxy with weighted upstreams.

    Why MLflow serving and not BentoML: the registered artifact is already an MLflow pyfunc,
    so `mlflow models serve -m models:/<name>@<alias>` serves exactly what the registry holds
    with no second packaging step, no bento build and no extra image family. BentoML would add
    a bentofile, its own store and a build per version for a course whose serving contract is
    one JSON endpoint. The trade: MLflow serving is a single-model Flask process with no
    autoscaling, which the canary by replica weight makes visible rather than hides.

    Two replicas run per environment, `serving-stable` on the alias `live` and
    `serving-canary` on the alias `canary`; the proxy splits traffic by the weights in
    `deploy/local/proxy/upstream.conf`. `deploy(live=False)` puts a version on the canary
    replica with `canary_percent` of traffic (zero means reachable only by its own address,
    the tenant's endpoint); `deploy(live=True)` moves the alias `live`, restarts the stable
    replica and sends everything back to it. Weights change with a reload, never a restart."""

    def __init__(
        self,
        registry: LocalModelRegistry,
        proxy_dir: Path,
        url: str,
        compose: ComposeRunner,
        *,
        stable: str = "serving-stable",
        canary: str = "serving-canary",
        proxy_service: str = "proxy",
        timeout_s: float = 30.0,
    ) -> None:
        self.registry = registry
        self.proxy_dir = Path(proxy_dir)
        self.url = url.rstrip("/")
        self.compose = compose
        self.stable = stable
        self.canary = canary
        self.proxy_service = proxy_service
        self.timeout_s = timeout_s

    # weights
    def weights(self) -> Weights:
        path = self.proxy_dir / "upstream.conf"
        if not path.exists():
            return Weights(100, 0)
        return parse_upstream(path.read_text(encoding="utf-8"))

    def set_weights(self, canary_percent: int) -> Weights:
        self.proxy_dir.mkdir(parents=True, exist_ok=True)
        (self.proxy_dir / "upstream.conf").write_text(
            render_upstream(canary_percent, self.stable, self.canary), encoding="utf-8"
        )
        w = Weights(100 - int(canary_percent), int(canary_percent))
        (self.proxy_dir / "weights.json").write_text(
            json.dumps({"stable": w.stable, "canary": w.canary}) + "\n"
        )
        self.compose.run("exec", "-T", self.proxy_service, "nginx", "-s", "reload")
        log.info("weights", extra={"stable": w.stable, "canary": w.canary})
        return w

    # protocol
    def deploy(
        self, tenant: Tenant, version: ModelVersion, *, live: bool = False, canary_percent: int = 0
    ) -> str:
        if live:
            self.registry.set_stage(
                tenant, version.name, version.version, Stage.LIVE, "deployed live"
            )
            self.compose.run("restart", self.stable)
            self.set_weights(0)
            return f"{self.url}/invocations"
        client = _client(self.registry.uri)
        client.set_registered_model_alias(
            self.registry.model_name(tenant, version.name), CANARY_ALIAS, version.version
        )
        self.compose.run("restart", self.canary)
        self.set_weights(canary_percent)
        return f"{self.url}/invocations"

    def invoke(self, tenant: Tenant, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json", "X-Tenant": tenant.prefix}
        r = httpx.post(
            f"{self.url}/invocations",
            json={"dataframe_records": [dict(payload)]},
            headers=headers,
            timeout=self.timeout_s,
        )
        r.raise_for_status()
        body = r.json()
        predictions = body.get("predictions", body) if isinstance(body, dict) else body
        first = predictions[0] if isinstance(predictions, list) and predictions else predictions
        return {"prediction": first, "replica": r.headers.get("x-upstream")}

    def status(self, tenant: Tenant, name: str) -> Mapping[str, Any]:
        w = self.weights()
        live = self.registry.live(tenant, name)
        canary = self.registry.by_alias(tenant, name, CANARY_ALIAS)
        out: dict[str, Any] = {
            "endpoint": f"{self.url}/invocations",
            "weights": {"stable": w.stable, "canary": w.canary},
            "live": live.version if live else None,
            "canary": canary.version if canary else None,
        }
        try:
            import httpx

            out["proxy"] = httpx.get(f"{self.url}/healthz", timeout=3.0).status_code == 200
        except Exception:  # noqa: BLE001
            out["proxy"] = False
        return out

    def delete(self, tenant: Tenant, name: str) -> None:
        client = _client(self.registry.uri)
        try:
            client.delete_registered_model_alias(
                self.registry.model_name(tenant, name), CANARY_ALIAS
            )
        except Exception:  # noqa: BLE001  nothing on the canary
            pass
        self.set_weights(0)


# ----- PromptStore -------------------------------------------------------------------


class LocalPromptStore:
    """MLflow's prompt registry as the store of record for `nw/llm/prompts`: every version
    carries the tag `sha256_12`, the same twelve characters `Prompt.hash` computes, so the
    `name@hash` that travels in answers and spans resolves to one registered version.
    Registering the same text twice returns the existing version."""

    def __init__(self, uri: str) -> None:
        self.uri = uri

    def prompt_name(self, tenant: Tenant, name: str) -> str:
        return tenant.resource(f"prompt-{name}")

    def _to_version(self, name: str, pv: Any) -> PromptVersion:
        tags = dict(pv.tags or {})
        aliases = list(getattr(pv, "aliases", None) or [])
        return PromptVersion(
            name=name,
            version=str(pv.version),
            text=pv.template if isinstance(pv.template, str) else json.dumps(pv.template),
            sha256_12=tags.get("sha256_12") or prompt_hash(pv.template),
            stage=_stage_of(aliases, tags.get("stage")),
            tags=tags,
        )

    def register(
        self, tenant: Tenant, name: str, text: str, tags: Mapping[str, str]
    ) -> PromptVersion:
        digest = prompt_hash(text)
        for existing in self.versions(tenant, name):
            if existing.sha256_12 == digest:
                return existing
        client = _client(self.uri)
        pv = client.register_prompt(
            name=self.prompt_name(tenant, name),
            template=text,
            commit_message=f"{name}@{digest}",
            tags={
                "sha256_12": digest,
                "stage": Stage.CANDIDATE.value,
                "tenant": tenant.name,
                "registered_at": _now(),
                **{k: str(v) for k, v in tags.items()},
            },
        )
        client.set_prompt_alias(
            self.prompt_name(tenant, name), Stage.CANDIDATE.value, int(pv.version)
        )
        return self._to_version(
            name, client.get_prompt_version(self.prompt_name(tenant, name), pv.version)
        )

    def get(self, tenant: Tenant, name: str, version: str | None = None) -> PromptVersion:
        client = _client(self.uri)
        full = self.prompt_name(tenant, name)
        if version is not None:
            pv = client.get_prompt_version(full, version)
            if pv is None:
                raise KeyError(f"prompt {name} has no version {version}")
            return self._to_version(name, pv)
        found = self.versions(tenant, name)
        if not found:
            raise KeyError(f"prompt {name} is not registered for {tenant.prefix}")
        return default_prompt(found)

    def set_stage(self, tenant: Tenant, name: str, version: str, stage: Stage) -> PromptVersion:
        client = _client(self.uri)
        full = self.prompt_name(tenant, name)
        current = client.get_prompt_version(full, version)
        if current is None:
            raise KeyError(f"prompt {name} has no version {version}")
        if stage in UNIQUE_STAGES:
            for other in self.versions(tenant, name):
                if other.stage == stage and other.version != str(version):
                    client.set_prompt_version_tag(
                        full, other.version, "stage", STEP_BACK[stage].value
                    )
        for old in list(getattr(current, "aliases", None) or []):
            if old in {s.value for s in Stage} and old != stage.value:
                client.delete_prompt_alias(full, old)
        client.set_prompt_alias(full, stage.value, int(version))
        client.set_prompt_version_tag(full, version, "stage", stage.value)
        client.set_prompt_version_tag(full, version, "stage_at", _now())
        return self._to_version(name, client.get_prompt_version(full, version))

    def versions(self, tenant: Tenant, name: str) -> Sequence[PromptVersion]:
        client = _client(self.uri)
        full = self.prompt_name(tenant, name)
        try:
            page = client.search_prompt_versions(full)
        except Exception:  # noqa: BLE001  not registered yet
            return []
        found = getattr(page, "prompt_versions", None)
        if found is None:
            found = list(page)
        out = []
        for pv in found:
            full_pv = client.get_prompt_version(full, pv.version)
            out.append(self._to_version(name, full_pv or pv))
        return sorted(out, key=lambda v: int(v.version))


# ----- VectorStore -------------------------------------------------------------------


def make_embedder(kind: str = "hash") -> Callable[[Sequence[str]], list[list[float]]]:
    """`hash` is the deterministic bag-of-words stand-in from the policy retriever (no
    download); any other value is a sentence-transformers model name."""
    if kind == "hash":
        from nw.policy.retrieval import HashEmbeddings

        h = HashEmbeddings()
        return lambda texts: h.encode(list(texts)).tolist()
    from sentence_transformers import SentenceTransformer

    from nw.config import hf_revision

    model = SentenceTransformer(kind, revision=hf_revision(kind))
    return lambda texts: model.encode(list(texts), normalize_embeddings=True).tolist()


def point_id(external_id: str) -> str:
    """Qdrant ids are integers or UUIDs; chunk ids like `sla-2023#3` map to a stable UUID and
    travel in the payload."""
    return str(uuid.uuid5(_QDRANT_NS, external_id))


class LocalVectorStore:
    """Qdrant, one collection per tenant and corpus, cosine distance, payload with the text
    and the metadata so a hit reads like the policy retriever's `Retrieved`."""

    def __init__(
        self, client: Any, embed: Callable[[Sequence[str]], list[list[float]]] | None = None
    ) -> None:
        self.client = client
        self.embed = embed or make_embedder("hash")

    @classmethod
    def from_url(cls, url: str, embedder: str = "hash") -> LocalVectorStore:
        from qdrant_client import QdrantClient

        return cls(QdrantClient(url=url, timeout=30), make_embedder(embedder))

    @classmethod
    def in_memory(
        cls, embed: Callable[[Sequence[str]], list[list[float]]] | None = None
    ) -> LocalVectorStore:
        from qdrant_client import QdrantClient

        return cls(QdrantClient(":memory:"), embed)

    def collection_name(self, tenant: Tenant, collection: str) -> str:
        return tenant.resource(collection)

    def _ensure(self, name: str, dim: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(name):
            self.client.create_collection(
                name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
            )

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int:
        from qdrant_client.models import PointStruct

        if not (len(ids) == len(texts) == len(metadata)):
            raise ValueError("ids, texts and metadata must have the same length")
        if not ids:
            return 0
        vecs = [list(map(float, v)) for v in vectors] if vectors is not None else self.embed(texts)
        if len(vecs) != len(ids):
            raise ValueError("one vector per id")
        name = self.collection_name(tenant, collection)
        self._ensure(name, len(vecs[0]))
        points = [
            PointStruct(id=point_id(i), vector=v, payload={"id": i, "text": t, **dict(m)})
            for i, t, v, m in zip(ids, texts, vecs, metadata, strict=True)
        ]
        for start in range(0, len(points), 256):
            self.client.upsert(name, points=points[start : start + 256], wait=True)
        return len(points)

    def search(
        self,
        tenant: Tenant,
        collection: str,
        query: str,
        k: int = 8,
        vector: Sequence[float] | None = None,
    ) -> Sequence[Hit]:
        name = self.collection_name(tenant, collection)
        if not self.client.collection_exists(name):
            return []
        q = list(map(float, vector)) if vector is not None else self.embed([query])[0]
        res = self.client.query_points(name, query=q, limit=k, with_payload=True)
        hits = []
        for p in res.points:
            payload = dict(p.payload or {})
            hits.append(
                Hit(
                    id=str(payload.pop("id", p.id)),
                    text=str(payload.pop("text", "")),
                    score=clamp_score(float(p.score)),
                    metadata={**payload, "raw_score": float(p.score), "score_kind": "cosine"},
                )
            )
        return hits

    def count(self, tenant: Tenant, collection: str) -> int:
        name = self.collection_name(tenant, collection)
        if not self.client.collection_exists(name):
            return 0
        return int(self.client.count(name, exact=True).count)

    def drop(self, tenant: Tenant, collection: str) -> None:
        name = self.collection_name(tenant, collection)
        if self.client.collection_exists(name):
            self.client.delete_collection(name)


# ----- AgentRuntime ------------------------------------------------------------------


class LocalAgentRuntime:
    """The agent container of the compose stack as the runtime (`resolver`, serving the
    AgentCore and Agent Engine contracts from `nw.agent.agentcore`).

    deploy: write the tenant's env file, recreate the container on the image given. invoke:
    HTTP to `/invocations`, the session id as a header. register: the card into
    `deploy/local/registry/<environment>-<tenant>.json` plus tags on a registered model
    `<prefix>-agent` in MLflow, so the registry UI lists agents beside models and prompts."""

    def __init__(
        self,
        url: str,
        registry_dir: Path,
        compose: ComposeRunner,
        mlflow_uri: str | None,
        *,
        service: str = "resolver",
        api_key: str | None = None,
        timeout_s: float = 120.0,
        transport: Any = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.registry_dir = Path(registry_dir)
        self.compose = compose
        self.mlflow_uri = mlflow_uri
        self.service = service
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.transport = transport

    def _headers(self, tenant: Tenant, session_id: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json", "X-Tenant": tenant.prefix}
        if self.api_key:
            h["x-api-key"] = self.api_key
        if session_id:
            h["X-Session-Id"] = session_id
        return h

    def _http(self) -> Any:
        import httpx

        return httpx.Client(timeout=self.timeout_s, transport=self.transport)

    def env_file(self, tenant: Tenant) -> Path:
        return self.registry_dir / f"{tenant.prefix}-agent.env"

    def card_path(self, tenant: Tenant) -> Path:
        return self.registry_dir / f"{tenant.prefix}.json"

    def deploy(self, tenant: Tenant, image: str, env: Mapping[str, str], *, version: str) -> str:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        lines = {
            "NW_TENANT": tenant.name,
            "NW_ENVIRONMENT": tenant.environment,
            "NW_AGENT_VERSION": version,
            **{k: str(v) for k, v in env.items()},
        }
        self.env_file(tenant).write_text(
            "".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8"
        )
        result = self.compose.run(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            self.service,
            env={"NW_AGENT_IMAGE": image, "NW_AGENT_ENV_FILE": str(self.env_file(tenant))},
        )
        if result.returncode != 0:
            raise RuntimeError(f"compose up {self.service} failed: {result.stderr.strip()[-400:]}")
        runtime_id = f"local://{self.service}/{version}"
        log.info(
            "agent deployed", extra={"image": image, "version": version, "runtime": runtime_id}
        )
        return runtime_id

    def invoke(
        self, tenant: Tenant, payload: Mapping[str, Any], *, session_id: str | None = None
    ) -> Mapping[str, Any]:
        with self._http() as http:
            r = http.post(
                f"{self.url}/invocations",
                json=dict(payload),
                headers=self._headers(tenant, session_id),
            )
            r.raise_for_status()
            return r.json()

    def register(self, tenant: Tenant, card: Mapping[str, Any]) -> str:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        version = str(card.get("agent_version") or "unknown")
        runtime_id = f"local://agents/{tenant.prefix}/{version}"
        record = {
            **dict(card),
            "runtime_id": runtime_id,
            "tenant": tenant.name,
            "registered_at": _now(),
        }
        self.card_path(tenant).write_text(
            json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8"
        )
        if self.mlflow_uri:
            client = _client(self.mlflow_uri)
            model_name = tenant.resource("agent")
            try:
                client.create_registered_model(model_name)
            except Exception:  # noqa: BLE001  exists
                pass
            tags = {
                "kind": "agent",
                "agent_version": version,
                "prompt": str(card.get("prompt", "")),
                "tools_version": str(card.get("tools_version", "")),
                "use_case": str(card.get("use_case", "")),
                "owner": str(card.get("owner", "")),
                "risk_class": str(card.get("risk_class", "")),
                "approval_state": str((card.get("approval") or {}).get("state", "")),
                "runtime_id": runtime_id,
                "card": str(self.card_path(tenant)),
                "registered_at": record["registered_at"],
            }
            for k, v in tags.items():
                client.set_registered_model_tag(model_name, k, v)
        log.info("agent registered", extra={"tenant": tenant.prefix, "runtime": runtime_id})
        return runtime_id

    def status(self, tenant: Tenant) -> Mapping[str, Any]:
        out: dict[str, Any] = {"url": self.url, "card": self.card_path(tenant).exists()}
        try:
            with self._http() as http:
                ping = http.get(f"{self.url}/ping", headers=self._headers(tenant))
                out["ping"] = (
                    ping.json()
                    if ping.headers.get("content-type", "").startswith("application/json")
                    else ping.text
                )
                out["healthy"] = ping.status_code == 200
                version = http.get(f"{self.url}/version", headers=self._headers(tenant))
                if version.status_code == 200:
                    out["version"] = version.json()
        except Exception as exc:  # noqa: BLE001
            out["healthy"] = False
            out["error"] = str(exc)
        return out


# ----- the factory -------------------------------------------------------------------


def build(
    settings: Settings, config: LocalConfig | None = None, compose: ComposeRunner | None = None
) -> Platform:
    """Everything the Local track offers, from `NW_*` (see `LocalConfig.from_env`)."""
    cfg = config or LocalConfig.from_env(settings)
    runner = compose or ComposeRunner(cfg.project_dir)
    registry = LocalModelRegistry(cfg.mlflow_uri)
    return Platform(
        track=Track.LOCAL,
        registry=registry,
        pipelines=LocalPipelineRunner(cfg.pipeline_root, cfg.kfp_runner),
        endpoints=LocalEndpointClient(registry, cfg.proxy_dir, cfg.endpoint_url, runner),
        prompts=LocalPromptStore(cfg.mlflow_uri),
        vectors=LocalVectorStore.from_url(cfg.qdrant_url, cfg.embedder),
        agents=LocalAgentRuntime(
            cfg.agent_url, cfg.registry_dir, runner, cfg.mlflow_uri, api_key=cfg.api_key
        ),
        gateway_url=cfg.gateway_url,
    )


# ----- CLI -----------------------------------------------------------------------------


@dataclass
class _Ctx:
    settings: Settings
    tenant: Tenant
    platform: Platform
    cfg: LocalConfig
    compose: ComposeRunner
    higher: Tenant = field(init=False)

    def __post_init__(self) -> None:
        self.higher = Tenant(self.tenant.name, HIGHER_ENVIRONMENT)


def _ctx() -> _Ctx:
    from nw.platform.base import tenant_from_env

    s = Settings()
    cfg = LocalConfig.from_env(s)
    compose = ComposeRunner(cfg.project_dir)
    return _Ctx(s, tenant_from_env(s), build(s, cfg, compose), cfg, compose)


def cmd_status(ctx: _Ctx, args: argparse.Namespace) -> int:
    p, t = ctx.platform, ctx.tenant
    rows: list[tuple[str, str]] = [
        ("track", "local"),
        ("tenant", t.prefix),
        ("mlflow", ctx.cfg.mlflow_uri),
    ]
    try:
        live = p.registry.live(t, args.name)
        rows.append((f"{args.name} live", f"version {live.version}" if live else "none"))
        higher = p.registry.live(ctx.higher, args.name)
        rows.append(
            (
                f"{args.name} live in {HIGHER_ENVIRONMENT}",
                f"version {higher.version}" if higher else "none",
            )
        )
    except Exception as exc:  # noqa: BLE001
        rows.append(("registry", f"unreachable: {exc}"))
    st = p.endpoints.status(t, args.name)
    rows.append(("weights", f"stable {st['weights']['stable']} canary {st['weights']['canary']}"))
    rows.append(("proxy", "up" if st.get("proxy") else "down"))
    ag = p.agents.status(t)
    rows.append(("agent runtime", "healthy" if ag.get("healthy") else "down"))
    rows.append(("gateway", p.gateway_url or "direct"))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k:<{width}}  {v}")
    return 0


def cmd_canary(ctx: _Ctx, args: argparse.Namespace) -> int:
    endpoints = ctx.platform.endpoints
    assert isinstance(endpoints, LocalEndpointClient)
    w = endpoints.set_weights(args.weight)
    print(f"stable {w.stable} percent, canary {w.canary} percent")
    return 0


def cmd_bootstrap(ctx: _Ctx, args: argparse.Namespace) -> int:
    """`nw.platform.bootstrap` (register and approve the promoted artifact, `source=bootstrap`),
    then live on the stack's serving tier."""
    from nw.platform.bootstrap import bootstrap

    p, t = ctx.platform, ctx.tenant
    if p.registry.live(t, args.name) and not args.force:
        print(f"{args.name}: a live version exists for {t.prefix}; nothing to do")
        return 0
    artifact = Path(args.artifact)
    if not (artifact / "metadata.json").exists():
        print(f"{artifact} is missing: run `make train-{args.name}` first")
        return 1
    v, _ = bootstrap(p.registry, t, args.name, artifact=artifact, force=True)
    p.endpoints.deploy(t, v, live=True)
    print(f"{args.name}: version {v.version} registered and live for {t.prefix}")
    return 0


def cmd_promote(ctx: _Ctx, args: argparse.Namespace) -> int:
    p, t = ctx.platform, ctx.tenant
    registry = p.registry
    assert isinstance(registry, LocalModelRegistry)
    live = registry.live(t, args.name)
    if live is None:
        print(f"{args.name}: nothing is live for {t.prefix}; promote after a gate has passed")
        return 1
    target = Tenant(t.name, args.to)
    copied = registry.copy_to(t, args.name, live, target)
    registry.set_stage(
        target, args.name, copied.version, Stage.LIVE, f"promoted from {live.uri} by {args.by}"
    )
    ctx.compose.run("restart", f"serving-stable-{args.to}")
    dst = registry.model_name(target, args.name)
    print(f"{args.name}: {live.uri} is live in {args.to} as version {copied.version} of {dst}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["_execute"] and len(raw) == 2:
        return execute_run(Path(raw[1]))  # the child process of LocalPipelineRunner.submit
    ap = argparse.ArgumentParser(prog="nw.platform.local", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="what is live, the weights, the agent runtime")
    s.add_argument("--name", default="triage")
    s.set_defaults(fn=cmd_status)
    c = sub.add_parser("canary", help="percent of traffic to the canary replica")
    c.add_argument("weight", type=int)
    c.set_defaults(fn=cmd_canary)
    b = sub.add_parser("bootstrap", help="register the promoted local artifact and make it live")
    b.add_argument("--name", default="triage")
    b.add_argument("--artifact", default="artifacts/triage/latest")
    b.add_argument("--force", action="store_true")
    b.set_defaults(fn=cmd_bootstrap)
    pr = sub.add_parser("promote", help="copy the live version into a higher environment")
    pr.add_argument("--name", default="triage")
    pr.add_argument("--to", default=HIGHER_ENVIRONMENT)
    pr.add_argument("--by", default=os.environ.get("USER", "operator"))
    pr.set_defaults(fn=cmd_promote)
    args = ap.parse_args(raw)
    return args.fn(_ctx(), args)


if __name__ == "__main__":
    sys.exit(main())
