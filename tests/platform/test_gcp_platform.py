"""nw.platform.gcp against fakes: no SDK, no network. Each fake implements the few calls the
implementation makes, so a test failing here means the implementation changed what it asks
of the Agent Platform."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nw.config import Settings, Track
from nw.platform import gcp
from nw.platform.base import (
    AgentRuntime,
    EndpointClient,
    ModelRegistry,
    PipelineRunner,
    PromptStore,
    RunStatus,
    Stage,
    Tenant,
    VectorStore,
    platform_for,
)

# ----- fakes -----------------------------------------------------------------------------------


class PreconditionFailed(Exception):
    """Named like google.api_core's: `if_generation_match` did not match."""

    code = 412


class FakeBlob:
    """A GCS object with a generation per write, honouring `if_generation_match` (0 means the
    object must not exist) so the read-modify-write documents are tested for lost updates."""

    def __init__(self, store: dict[str, str], gens: dict[str, int], name: str) -> None:
        self.store, self.gens, self.name = store, gens, name
        self.generation: int | None = None

    def exists(self) -> bool:
        return self.name in self.store

    def reload(self) -> None:
        self.generation = self.gens.get(self.name)

    def download_as_text(self, if_generation_match: int | None = None) -> str:
        if if_generation_match is not None and self.gens.get(self.name) != if_generation_match:
            raise PreconditionFailed(self.name)
        return self.store[self.name]

    def upload_from_string(
        self, text: str, content_type: str = "", if_generation_match: int | None = None
    ) -> None:
        if if_generation_match is not None and self.gens.get(self.name, 0) != if_generation_match:
            raise PreconditionFailed(self.name)
        self.store[self.name] = text
        self.gens[self.name] = self.gens.get(self.name, 0) + 1

    def upload_from_filename(self, path: str) -> None:
        self.upload_from_string(Path(path).read_bytes().decode("latin-1"))

    def download_to_filename(self, path: str) -> None:
        Path(path).write_text(self.store[self.name])

    def delete(self) -> None:
        self.store.pop(self.name, None)
        self.gens.pop(self.name, None)


class FakeBucket:
    def __init__(self, store: dict[str, str], gens: dict[str, int]) -> None:
        self.store, self.gens = store, gens

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, self.gens, name)

    def list_blobs(self, prefix: str = "") -> list[FakeBlob]:
        return [self.blob(k) for k in sorted(self.store) if k.startswith(prefix)]


class FakeStorage:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, str]] = {}
        self.gens: dict[str, dict[str, int]] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.buckets.setdefault(name, {}), self.gens.setdefault(name, {}))


class FakeVersion:
    def __init__(self, version_id: str, aliases: list[str], description: str = "") -> None:
        self.version_id = version_id
        self.version_aliases = aliases
        self.version_description = description


class FakeVersioningRegistry:
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def list_versions(self) -> list[FakeVersion]:
        return list(self.model.versions)

    def add_version_aliases(self, version_name: str, alias_names: list[str]) -> None:
        for v in self.model.versions:  # aliases are unique per model: they move
            for a in alias_names:
                if a in v.version_aliases:
                    v.version_aliases.remove(a)
        for v in self.model.versions:
            if v.version_id == version_name:
                v.version_aliases.extend(alias_names)

    def remove_version_aliases(self, version_name: str, alias_names: list[str]) -> None:
        for v in self.model.versions:
            if v.version_id == version_name:
                v.version_aliases = [a for a in v.version_aliases if a not in alias_names]


class FakeModel:
    def __init__(self, display_name: str, resource_name: str) -> None:
        self.display_name = display_name
        self.resource_name = resource_name
        self.versions: list[FakeVersion] = []
        self.labels: dict[str, str] = {}
        self.uri = ""
        self.versioning_registry = FakeVersioningRegistry(self)
        self.deployed: list[dict[str, Any]] = []

    @property
    def version_id(self) -> str:
        return self.versions[-1].version_id

    @property
    def version_aliases(self) -> list[str]:
        return self.versions[-1].version_aliases

    @property
    def version_description(self) -> str:
        return self.versions[-1].version_description

    def deploy(self, **kw: Any) -> None:
        self.deployed.append(kw)
        endpoint = kw["endpoint"]
        endpoint.add(kw["deployed_model_display_name"], kw["traffic_percentage"])


class FakeEndpoint:
    """A Vertex endpoint: deployed models with ids, a traffic split that sums to 100, and
    `undeploy` refusing a model that still has traffic (as the SDK does)."""

    def __init__(self, resource_name: str) -> None:
        self.resource_name = resource_name
        self.models: dict[str, str] = {}  # id -> display name
        self.traffic_split: dict[str, int] = {}
        self.undeployed_ids: list[str] = []
        self.undeployed = False
        self._next = 0

    def add(self, display: str, percent: int) -> None:
        self._next += 1
        new = f"m{self._next}"
        if not self.traffic_split:
            percent = 100
        rest = 100 - percent
        total = sum(self.traffic_split.values()) or 1
        self.traffic_split = {k: round(v * rest / total) for k, v in self.traffic_split.items()}
        self.traffic_split[new] = percent
        self.models[new] = display

    def list_models(self) -> list[Any]:
        return [
            SimpleNamespace(id=i, display_name=d, model=f"models/{i}")
            for i, d in self.models.items()
        ]

    def update(self, traffic_split: dict[str, int]) -> None:
        assert sum(traffic_split.values()) == 100 and set(traffic_split) <= set(self.models)
        self.traffic_split = {i: traffic_split.get(i, 0) for i in self.models}

    def undeploy(self, deployed_model_id: str) -> None:
        assert self.traffic_split.get(deployed_model_id, 0) == 0, "undeploy a serving model"
        self.models.pop(deployed_model_id)
        self.traffic_split.pop(deployed_model_id, None)
        self.undeployed_ids.append(deployed_model_id)

    def predict(self, instances: list[Any]) -> Any:
        return SimpleNamespace(
            predictions=[{"queue": "billing"} for _ in instances], deployed_model_id="m1"
        )

    def undeploy_all(self) -> None:
        self.undeployed = True
        self.models.clear()
        self.traffic_split.clear()


class FakePipelineJob:
    jobs: dict[str, FakePipelineJob] = {}

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.resource_name = f"projects/p/locations/us-central1/pipelineJobs/{kw['display_name']}-1"
        self.state = SimpleNamespace(name="PIPELINE_STATE_PENDING")
        self.submitted_as: str | None = None
        FakePipelineJob.jobs[self.resource_name] = self

    def submit(self, service_account: str | None = None, **kw: Any) -> None:
        self.submitted_as = service_account
        self.state = SimpleNamespace(name="PIPELINE_STATE_RUNNING")

    @classmethod
    def get(cls, name: str) -> FakePipelineJob:
        return cls.jobs[name]


class FakeAiplatform:
    """The slice of google.cloud.aiplatform the registry, pipelines and endpoints use."""

    def __init__(self) -> None:
        self.models: dict[str, FakeModel] = {}
        self.endpoints: dict[str, FakeEndpoint] = {}
        self.PipelineJob = FakePipelineJob
        outer = self

        class Model:
            @staticmethod
            def list(filter: str = "") -> list[FakeModel]:
                display = filter.split('"')[1]
                return [m for m in outer.models.values() if m.display_name == display]

            @staticmethod
            def upload(**kw: Any) -> FakeModel:
                display = kw["display_name"]
                model = outer.models.get(display)
                if model is None or kw.get("parent_model") is None:
                    model = FakeModel(
                        display, f"projects/p/locations/us-central1/models/{len(outer.models) + 1}"
                    )
                    outer.models[display] = model
                model.versions.append(
                    FakeVersion(
                        str(len(model.versions) + 1),
                        list(kw["version_aliases"]),
                        kw["version_description"],
                    )
                )
                model.labels = dict(kw["labels"])
                model.uri = kw["artifact_uri"]
                model.serving_image = kw["serving_container_image_uri"]
                return model

            def __new__(cls, name: str) -> FakeModel:  # aiplatform.Model("resource@version")
                base, _, version = name.partition("@")
                model = next(m for m in outer.models.values() if m.resource_name == base)
                return model

        class Endpoint:
            def __new__(cls, name: str) -> FakeEndpoint:
                return outer.endpoints.setdefault(name, FakeEndpoint(name))

        self.Model = Model
        self.Endpoint = Endpoint


class FakeRunService:
    def __init__(self, name: str, env: dict[str, str]) -> None:
        self.name = name
        self.uri = f"https://{name.rsplit('/', 1)[-1]}-abc.a.run.app"
        self.latest_ready_revision = f"{name}/revisions/r1"
        self.template = SimpleNamespace(
            containers=[
                SimpleNamespace(env=[SimpleNamespace(name=k, value=v) for k, v in env.items()])
            ]
        )


class FakeRun:
    def __init__(self) -> None:
        self.services: dict[str, FakeRunService] = {}
        self.updated: list[FakeRunService] = []
        self.deleted: list[str] = []

    def get_service(self, name: str) -> FakeRunService:
        return self.services[name]

    def update_service(self, service: FakeRunService) -> Any:
        self.updated.append(service)
        return SimpleNamespace(result=lambda: service)

    def delete_service(self, name: str) -> Any:
        self.deleted.append(name)
        return SimpleNamespace(result=lambda: None)


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url: str, json: dict, headers: dict) -> Any:
        self.calls.append((url, json, headers))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True, "url": url})


class FakePrompts:
    """vertexai.preview.prompts: Prompt and create_version."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    @staticmethod
    def Prompt(prompt_data: str, prompt_name: str) -> Any:
        return SimpleNamespace(prompt_data=prompt_data, prompt_name=prompt_name)

    def create_version(
        self, prompt: Any, prompt_id: str | None = None, version_name: str | None = None
    ) -> Any:
        self.created.append(
            {"prompt": prompt.prompt_name, "prompt_id": prompt_id, "version_name": version_name}
        )
        return SimpleNamespace(
            prompt_id=prompt_id or f"{len(self.created)}", version_id=str(len(self.created))
        )


class FakeRag:
    def __init__(self) -> None:
        self.corpora: list[Any] = []
        self.files: dict[str, list[Any]] = {}
        self.imports: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def list_corpora(self) -> list[Any]:
        return list(self.corpora)

    def create_corpus(self, display_name: str, **kw: Any) -> Any:
        corpus = SimpleNamespace(
            display_name=display_name,
            name=f"projects/p/locations/us-central1/ragCorpora/{len(self.corpora) + 1}",
        )
        self.corpora.append(corpus)
        return corpus

    def import_files(self, corpus_name: str, paths: list[str], **kw: Any) -> None:
        self.imports.append({"corpus": corpus_name, "paths": paths, **kw})
        self.files.setdefault(corpus_name, []).extend(
            SimpleNamespace(name=f"{corpus_name}/ragFiles/{p}") for p in paths
        )

    def list_files(self, corpus_name: str) -> list[Any]:
        return list(self.files.get(corpus_name, []))

    def delete_file(self, name: str) -> None:
        for k, v in self.files.items():
            self.files[k] = [f for f in v if f.name != name]

    def retrieval_query(
        self, rag_resources: list[Any], text: str, rag_retrieval_config: Any
    ) -> Any:
        self.queries.append(
            {"resources": rag_resources, "text": text, "top_k": rag_retrieval_config.top_k}
        )
        ctx = SimpleNamespace(
            text="Refunds within 30 days.",
            score=0.91,
            source_uri="gs://b/northwind-alice/rag/policies/refund-policy.txt",
        )
        return SimpleNamespace(contexts=SimpleNamespace(contexts=[ctx]))

    @staticmethod
    def TransformationConfig(chunking_config: Any) -> Any:
        return SimpleNamespace(chunking_config=chunking_config)

    @staticmethod
    def ChunkingConfig(chunk_size: int, chunk_overlap: int) -> Any:
        return SimpleNamespace(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    @staticmethod
    def RagResource(rag_corpus: str) -> Any:
        return SimpleNamespace(rag_corpus=rag_corpus)

    @staticmethod
    def RagRetrievalConfig(top_k: int) -> Any:
        return SimpleNamespace(top_k=top_k)


class FakeEngines:
    def __init__(self) -> None:
        self.engines: list[Any] = []
        self.updates: list[Any] = []

    def list_reasoning_engines(self, parent: str) -> list[Any]:
        return list(self.engines)

    def update_reasoning_engine(self, reasoning_engine: Any, update_mask: dict) -> Any:
        self.updates.append((reasoning_engine, update_mask))
        return SimpleNamespace(result=lambda: reasoning_engine)


class FakeExecution:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def query_reasoning_engine(self, request: dict) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            output={"ticket_id": request["input"].get("ticket_id"), "queue": "billing"}
        )


def engine(display_name: str, image: str = "img:1") -> Any:
    return SimpleNamespace(
        name=f"projects/p/locations/us-central1/reasoningEngines/{display_name}",
        display_name=display_name,
        update_time="2026-09-29T10:00:00Z",
        spec=SimpleNamespace(
            container_spec=SimpleNamespace(image_uri=image),
            deployment_spec=SimpleNamespace(env=[SimpleNamespace(name="NW_TENANT", value="alice")]),
        ),
    )


# ----- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def cfg() -> gcp.GcpConfig:
    return gcp.GcpConfig(
        project="p",
        region="us-central1",
        artifacts_bucket="arts",
        pipelines_bucket="pipes",
        pipeline_dir="unused",
    )


@pytest.fixture
def fakes() -> dict[str, Any]:
    return {
        "storage": FakeStorage(),
        "aiplatform": FakeAiplatform(),
        "run": FakeRun(),
        "http": FakeHttp(),
        "prompts": FakePrompts(),
        "rag": FakeRag(),
        "reasoning_engines": FakeEngines(),
        "reasoning_engine_execution": FakeExecution(),
        "logging": SimpleNamespace(
            list_entries=lambda **kw: [
                SimpleNamespace(timestamp="t1", payload={"message": "step train started"})
            ]
        ),
    }


@pytest.fixture
def clients(cfg: gcp.GcpConfig, fakes: dict[str, Any]) -> gcp.GcpClients:
    return gcp.GcpClients(cfg).set(**fakes)


@pytest.fixture
def alice() -> Tenant:
    return Tenant(name="alice")


# ----- tests ---------------------------------------------------------------------------------------


def test_build_returns_every_protocol(fakes: dict[str, Any]) -> None:
    settings = Settings(track=Track.GCP, gcp_project="p", _env_file=None)
    platform = gcp.build(
        settings, clients=gcp.GcpClients(gcp.GcpConfig.from_settings(settings, env={})).set(**fakes)
    )
    assert platform.track == Track.GCP
    assert isinstance(platform.registry, ModelRegistry)
    assert isinstance(platform.pipelines, PipelineRunner)
    assert isinstance(platform.endpoints, EndpointClient)
    assert isinstance(platform.prompts, PromptStore)
    assert isinstance(platform.vectors, VectorStore)
    assert isinstance(platform.agents, AgentRuntime)
    assert platform.cfg.artifacts_bucket == "northwind-p-artifacts"
    assert platform.describe()["registry"] == "VertexModelRegistry"


def test_platform_for_picks_gcp(monkeypatch: pytest.MonkeyPatch, fakes: dict[str, Any]) -> None:
    monkeypatch.setenv("NW_GCP_PROJECT", "p")
    settings = Settings(track=Track.GCP, gcp_project="p", _env_file=None)
    platform = platform_for(settings)
    assert type(platform).__name__ == "GcpPlatform"


def test_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "NW_GCP_PROJECT": "p",
        "NW_GCP_RUN_REGION": "europe-west4",
        "NW_ENVIRONMENT": "nw",
        "NW_GATEWAY_URL": "https://gw",
    }
    cfg = gcp.GcpConfig.from_settings(Settings(track=Track.GCP, _env_file=None), env=env)
    assert (cfg.project, cfg.region, cfg.environment, cfg.gateway_url) == (
        "p",
        "europe-west4",
        "nw",
        "https://gw",
    )
    assert cfg.live_endpoint_name("semantic").endswith("/endpoints/100001")
    with pytest.raises(KeyError):
        cfg.live_endpoint_id("policy")
    with pytest.raises(ValueError):
        gcp.GcpConfig.from_settings(Settings(track=Track.GCP, _env_file=None), env={})


def test_registry_register_stage_live_download(cfg, clients, fakes, alice, tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    artifact.mkdir()
    (artifact / "model.joblib").write_text("bytes")
    (artifact / "MODEL_CARD.md").write_text("card")
    reg = gcp.VertexModelRegistry(cfg, clients)

    v1 = reg.register(alice, "triage", artifact, {"f1": 0.91}, {"git": "abc"})
    assert v1.stage == Stage.CANDIDATE and v1.version == "1" and v1.metrics == {"f1": 0.91}
    assert v1.uri.startswith("gs://arts/northwind-alice/models/triage/")
    assert fakes["aiplatform"].models["northwind-alice-triage"].labels == {
        "tenant": "alice",
        "environment": "northwind",
        "git": "abc",
    }
    assert "northwind-alice/models/triage/" in next(iter(fakes["storage"].buckets["arts"]))

    v2 = reg.register(alice, "triage", artifact, {"f1": 0.93}, {})
    assert v2.version == "2" and len(reg.versions(alice, "triage")) == 2
    assert reg.live(alice, "triage") is None

    live = reg.set_stage(alice, "triage", "2", Stage.LIVE, "gate passed")
    assert live.stage == Stage.LIVE
    assert reg.live(alice, "triage").version == "2"
    assert reg.set_stage(alice, "triage", "1", Stage.LIVE, "rollback").version == "1"
    # the previous live version steps back to retired (the contract's rule), logged with why
    assert [v.stage for v in reg.versions(alice, "triage")] == [Stage.LIVE, Stage.RETIRED]
    trail = (
        fakes["storage"]
        .buckets["arts"]["northwind-alice/registry/triage/stages.jsonl"]
        .splitlines()
    )
    assert [json.loads(r)["reason"] for r in trail] == [
        "registered",
        "registered",
        "gate passed",
        "replaced by 1: rollback",
        "rollback",
    ]
    # each version keeps its own artifact, not the default version's
    assert [v.uri for v in reg.versions(alice, "triage")][0] == v1.uri

    into = reg.download(alice, v1, tmp_path / "dl")
    assert (into / "model.joblib").read_text() == "bytes" and (into / "MODEL_CARD.md").exists()
    with pytest.raises(KeyError):
        reg.set_stage(alice, "nothing", "1", Stage.APPROVED, "x")


def test_pipelines_submit_status_wait_logs(cfg, clients, fakes, alice, tmp_path: Path) -> None:
    template = tmp_path / "retrain-triage.yaml"
    template.write_text("pipelineInfo: {}")
    sleeps: list[float] = []
    bundle = _bundle(tmp_path)
    runner = gcp.VertexPipelineRunner(cfg, clients, sleep=sleeps.append, bundler=lambda: bundle)
    with pytest.raises(FileNotFoundError):
        runner.submit(alice, "retrain-triage", {})
    run = runner.submit(alice, "retrain-triage", {"template_path": str(template), "epochs": 2})
    job = FakePipelineJob.jobs[run.run_id]
    assert job.kw["display_name"] == "northwind-alice-retrain-triage"
    assert job.kw["pipeline_root"] == "gs://pipes/northwind-alice"
    values = dict(job.kw["parameter_values"])
    platform_env = json.loads(values.pop("platform_env"))
    assert values == {
        "epochs": 2,
        "tenant": "alice",
        "environment": "northwind",
        "output_root": "gs://arts/northwind-alice/pipelines/runs",
        "production_summary": "gs://arts/baselines/triage_production.json",
        "source_uri": f"gs://arts/northwind-alice/source/{bundle.name}",
    }
    # the learner's code rides with the run, and the scheduler's copy follows it
    arts = fakes["storage"].buckets["arts"]
    assert f"northwind-alice/source/{bundle.name}" in arts
    assert "northwind-alice/source/latest.tar.gz" in arts
    assert platform_env["NW_TRACK"] == "gcp" and platform_env["NW_GCP_PROJECT"] == "p"
    assert platform_env["NW_TENANT"] == "alice"
    # With the data bucket known, the tickets default to the deployed copy; a value passed wins.
    cfg.data_bucket = "dat"
    run2 = runner.submit(
        alice,
        "semantic",
        {"template_path": str(template), "production_summary": "gs://mine/p.json"},
    )
    values = FakePipelineJob.jobs[run2.run_id].kw["parameter_values"]
    assert values["data_uri"] == "gs://dat/tickets/tickets.jsonl"
    assert values["production_summary"] == "gs://mine/p.json"
    cfg.data_bucket = ""
    assert job.submitted_as == "nw-alice-pipelines@p.iam.gserviceaccount.com"
    assert run.status == RunStatus.RUNNING and "console.cloud.google.com" in run.url
    assert runner.status(alice, run).status == RunStatus.RUNNING
    job.state = SimpleNamespace(name="PIPELINE_STATE_SUCCEEDED")
    assert runner.wait(alice, run).status == RunStatus.SUCCEEDED
    job.state = SimpleNamespace(name="PIPELINE_STATE_RUNNING")
    with pytest.raises(TimeoutError):
        runner.wait(alice, run, timeout_s=0)
    assert list(runner.logs(alice, run)) == ["t1 step train started"]


def test_pipelines_named_template_is_uploaded_where_the_scheduler_reads_it(
    cfg, clients, fakes, alice, tmp_path: Path
) -> None:
    compiled = tmp_path / "pipelines"
    compiled.mkdir()
    (compiled / "retrain-triage.yaml").write_text("pipelineInfo: {name: northwind-triage}")
    cfg.pipeline_dir = str(compiled)
    runner = gcp.VertexPipelineRunner(
        cfg, clients, sleep=lambda s: None, bundler=lambda: _bundle(tmp_path)
    )
    uri = "gs://arts/northwind-alice/pipelines/retrain-triage.yaml"
    assert runner.template_uri(alice, "retrain-triage") == uri
    run = runner.submit(alice, "retrain-triage", {})
    assert FakePipelineJob.jobs[run.run_id].kw["template_path"] == uri
    stored = fakes["storage"].buckets["arts"]["northwind-alice/pipelines/retrain-triage.yaml"]
    assert "northwind-triage" in stored
    assert runner.upload(alice, "retrain-triage") == uri
    with pytest.raises(FileNotFoundError, match="make pipeline-compile"):
        runner.upload(alice, "retrain-semantic")

    # A hand submission of `triage` also refreshes what the weekly job reads.
    (compiled / "triage.yaml").write_text("pipelineInfo: {name: northwind-triage-v2}")
    runner.submit(alice, "triage", {})
    arts = fakes["storage"].buckets["arts"]
    assert "v2" in arts["northwind-alice/pipelines/triage.yaml"]
    assert "v2" in arts["northwind-alice/pipelines/retrain-triage.yaml"]


def test_the_scheduler_reads_the_uploaded_template_with_the_pipeline_parameters() -> None:
    """The Cloud Scheduler job and `VertexPipelineRunner.upload` agree on the object, and the
    job passes only parameters the compiled pipeline declares."""
    from nw.pipelines.params import BY_PIPELINE

    tf = (Path(__file__).parents[2] / "deploy/gcp/modules/tracking/main.tf").read_text()
    assert "${var.environment}-${each.key}/pipelines/retrain-triage.yaml" in tf
    block = tf.split("parameterValues = {", 1)[1].split("\n        }", 1)[0]
    values = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in block.splitlines()
        if "=" in line
    }
    assert set(values) <= {p.name for p in BY_PIPELINE["triage"]}
    # The same locations a hand submission defaults to (`VertexPipelineRunner.deployed_defaults`).
    assert {"data_uri", "output_root", "production_summary", "trigger"} <= set(values)
    assert values["trigger"] == '"schedule"'
    assert values["output_root"].endswith('${var.environment}-${each.key}/pipelines/runs"')
    data_tf = (Path(__file__).parents[2] / "deploy/gcp/modules/data/main.tf").read_text()
    assert 'name         = "baselines/${each.key}_production.json"' in data_tf


def test_endpoints_tenant_and_live(cfg, clients, fakes, alice) -> None:
    fakes["run"].services["projects/p/locations/us-central1/services/northwind-alice-triage"] = (
        FakeRunService(
            "projects/p/locations/us-central1/services/northwind-alice-triage",
            {"NW_TRACK": "gcp", "NW_MODEL_URI": ""},
        )
    )
    reg = fakes["aiplatform"]
    model = reg.Model.upload(
        display_name="northwind-alice-triage",
        artifact_uri="gs://arts/x",
        serving_container_image_uri="i",
        parent_model=None,
        version_aliases=["approved"],
        version_description="{}",
        labels={},
    )
    ep = gcp.CloudRunEndpointClient(cfg, clients, api_key="k")
    version = gcp.ModelVersion(name="triage", version="1", stage=Stage.APPROVED, uri="gs://arts/x")

    url = ep.deploy(alice, version)
    updated = fakes["run"].updated[0]
    env = {e.name: e.value for e in updated.template.containers[0].env}
    assert (
        env["NW_MODEL_URI"] == "gs://arts/x"
        and env["NW_MODEL_VERSION"] == "1"
        and url.endswith(".run.app")
    )

    assert ep.invoke(alice, "triage", {"subject": "s", "body": "b"})["ok"] is True
    url, body, headers = fakes["http"].calls[0]
    assert (
        url.endswith("/triage")
        and body == {"subject": "s", "body": "b"}
        and headers == {"x-api-key": "k"}
    )
    status = ep.status(alice, "triage")
    assert status["kind"] == "cloud-run" and status["model_version"] == "1"

    live = Tenant(name="live")
    name = ep.deploy(alice, version, live=True, canary_percent=10)
    assert name.endswith("/endpoints/100000")
    # nothing was serving: the first live version takes everything
    assert model.deployed[0]["endpoint"].resource_name == name
    assert ep.invoke(live, "triage", {"instances": [{"a": 1}]})["predictions"] == [
        {"queue": "billing"}
    ]
    assert ep.status(live, "triage")["traffic_split"] == {"m1": 100}
    assert ep.status(live, "triage")["stable"] == "m1"
    ep.delete(live, "triage")
    assert fakes["aiplatform"].endpoints[name].undeployed is True
    ep.delete(alice, "triage")
    assert fakes["run"].deleted == [
        "projects/p/locations/us-central1/services/northwind-alice-triage"
    ]


def test_prompts_register_get_stage_versions(cfg, clients, fakes, alice) -> None:
    store = gcp.VertexPromptStore(cfg, clients)
    a = store.register(alice, "policy.answer", "Answer from the policy.", {"owner": "policy"})
    assert a.stage == Stage.CANDIDATE and a.version == a.sha256_12 == gcp.prompt_hash(
        "Answer from the policy."
    )
    assert (
        store.register(alice, "policy.answer", "Answer from the policy.", {}).version == a.version
    )  # idempotent
    assert fakes["prompts"].created == [
        {"prompt": "northwind-alice-policy.answer", "prompt_id": None, "version_name": a.version}
    ]
    b = store.register(alice, "policy.answer", "Answer from the policy, cite it.", {})
    assert fakes["prompts"].created[1]["prompt_id"] == "1", (
        "the second version joins the same online prompt"
    )
    assert store.get(alice, "policy.answer").version == b.version, (
        "newest candidate when nothing is approved"
    )
    store.set_stage(alice, "policy.answer", a.version, Stage.LIVE)
    assert store.get(alice, "policy.answer").version == a.version
    store.set_stage(alice, "policy.answer", b.version, Stage.LIVE)
    stages = {v.version: v.stage for v in store.versions(alice, "policy.answer")}
    assert stages == {a.version: Stage.RETIRED, b.version: Stage.LIVE}
    assert store.get(alice, "policy.answer", a.version).text == "Answer from the policy."
    record = json.loads(
        fakes["storage"].buckets["arts"][f"northwind-alice/prompts/policy.answer/{a.version}.json"]
    )
    assert record["online"]["store"] == "vertexai.preview.prompts"
    with pytest.raises(KeyError):
        store.get(alice, "missing")


def test_prompts_without_the_sdk_use_the_bucket(cfg, clients, fakes, alice) -> None:
    clients.set(prompts=None)
    store = gcp.VertexPromptStore(cfg, clients)
    v = store.register(alice, "judge.rubric", "Score 1 to 5.", {})
    record = json.loads(
        fakes["storage"].buckets["arts"][f"northwind-alice/prompts/judge.rubric/{v.version}.json"]
    )
    assert record["online"] == {"store": "bucket"}


def test_vectors_upsert_search_count_drop(cfg, clients, fakes, alice) -> None:
    fakes["rag"].corpora.append(
        SimpleNamespace(
            display_name="northwind-alice-policies",
            name="projects/p/locations/us-central1/ragCorpora/7",
        )
    )
    vs = gcp.RagEngineVectorStore(cfg, clients)
    n = vs.upsert(
        alice,
        "policies",
        ["refund-policy", "sla#2"],
        ["Refunds within 30 days.", "SLA is 99.9."],
        None,
        [{"vintage": "2025", "audience": "internal"}, {"audience": "customer"}],
    )
    assert n == 2
    imp = fakes["rag"].imports[0]
    # only the documents are imported: metadata files never become corpus documents
    assert imp["corpus"].endswith("/7") and imp["paths"] == [
        "gs://arts/northwind-alice/rag/policies/docs/"
    ]
    arts = fakes["storage"].buckets["arts"]
    assert arts["northwind-alice/rag/policies/docs/refund-policy.txt"] == "Refunds within 30 days."
    assert "northwind-alice/rag/policies/docs/sla%232.txt" in arts
    assert not any(k.endswith(".meta.json") for k in arts)
    hits = vs.search(alice, "policies", "refund?", k=3)
    # the hit carries the audience it was stored with, so the service can filter it
    assert hits[0].id == "refund-policy" and hits[0].metadata["audience"] == "internal"
    assert hits[0].metadata["raw_score"] == 0.91 and hits[0].metadata["score_kind"] == "distance"
    assert hits[0].score == pytest.approx(1 - 0.91) and fakes["rag"].queries[0]["top_k"] == 3
    assert gcp.RagEngineVectorStore.id_of("gs://b/p/docs/sla%232.txt") == "sla#2"
    assert vs.count(alice, "policies") == 1
    vs.drop(alice, "policies")
    assert vs.count(alice, "policies") == 0
    # the source objects are gone too, so the next upsert cannot import them back
    assert not any(k.startswith("northwind-alice/rag/policies/") for k in arts)
    with pytest.raises(KeyError):
        vs.search(Tenant(name="bob"), "policies", "x")


def test_rag_hits_without_metadata_read_as_internal(cfg, clients, fakes, alice) -> None:
    """A hit whose id has no stored metadata fails closed in the policy retriever."""
    from nw.platform.retrievers import chunk_from_hit

    fakes["rag"].corpora.append(
        SimpleNamespace(display_name="northwind-alice-policies", name="c/7")
    )
    vs = gcp.RagEngineVectorStore(cfg, clients, score_kind="similarity")
    [hit] = vs.search(alice, "policies", "refund?")
    assert "audience" not in hit.metadata and hit.score == 0.91
    assert chunk_from_hit(hit).audience == "internal"


def test_live_canary_promote_undeploys_the_old_model(cfg, clients, fakes, alice) -> None:
    """The live endpoint keeps one stable model and one canary; finishing undeploys the rest,
    so no old replica keeps billing (audit 01 H11)."""
    reg = gcp.VertexModelRegistry(cfg, clients)
    ep = gcp.CloudRunEndpointClient(cfg, clients)
    live = Tenant(name="live")
    versions = []
    for _ in range(3):
        versions.append(_registered(reg, alice, fakes))
    ep.deploy(alice, versions[0], live=True)
    endpoint = fakes["aiplatform"].endpoints[cfg.live_endpoint_name("triage")]
    assert endpoint.traffic_split == {"m1": 100}
    ep.deploy(alice, versions[1], live=True, canary_percent=10)
    assert endpoint.traffic_split == {"m1": 90, "m2": 10}
    record = ep.live_record("triage")
    assert (record["stable"], record["canary"]) == ("m1", "m2")
    # finishing the same version never deploys a second copy
    ep.deploy(alice, versions[1], live=True, canary_percent=0)
    assert endpoint.traffic_split == {"m2": 100} and endpoint.undeployed_ids == ["m1"]
    assert len(fakes["aiplatform"].models["northwind-alice-triage"].deployed) == 2
    ep.deploy(alice, versions[2], live=True, canary_percent=20)
    assert ep.promote(live, "triage") == "m3"
    assert endpoint.traffic_split == {"m3": 100} and endpoint.undeployed_ids == ["m1", "m2"]
    with pytest.raises(KeyError, match="no canary"):
        ep.promote(live, "triage")


def test_live_rollback_undeploys_the_canary_and_one_canary_at_a_time(
    cfg, clients, fakes, alice
) -> None:
    reg = gcp.VertexModelRegistry(cfg, clients)
    ep = gcp.CloudRunEndpointClient(cfg, clients)
    live = Tenant(name="live")
    v1, v2, v3 = (_registered(reg, alice, fakes) for _ in range(3))
    ep.deploy(alice, v1, live=True)
    endpoint = fakes["aiplatform"].endpoints[cfg.live_endpoint_name("triage")]
    ep.deploy(alice, v2, live=True, canary_percent=50)
    # a second canary replaces the unfinished one instead of stacking a third model
    ep.deploy(alice, v3, live=True, canary_percent=10)
    assert set(endpoint.models) == {"m1", "m3"} and endpoint.undeployed_ids == ["m2"]
    assert ep.rollback(live, "triage") == "m1"
    assert endpoint.traffic_split == {"m1": 100} and endpoint.undeployed_ids == ["m2", "m3"]
    assert ep.status(live, "triage")["canary"] is None


def test_shared_documents_retry_instead_of_losing_an_update(cfg, clients, fakes) -> None:
    docs = gcp.Documents(clients, "arts")
    docs.write("agents/agents.json", {"agents": []})
    real = docs.update_text
    raced = {"done": False}

    def racing(path, mutate, content_type):
        def interleaved(text):
            if not raced["done"]:
                raced["done"] = True
                gcp.Documents(clients, "arts").update(
                    "agents/agents.json", lambda d: {**d, "agents": ["bob"]}, {}
                )
            return mutate(text)

        return real(path, interleaved, content_type)

    docs.update_text = racing  # type: ignore[method-assign]
    docs.update("agents/agents.json", lambda d: {**d, "agents": [*d["agents"], "alice"]}, {})
    assert json.loads(fakes["storage"].buckets["arts"]["agents/agents.json"])["agents"] == [
        "bob",
        "alice",
    ]


def _bundle(tmp_path: Path):
    from nw.pipelines.source import Bundle

    path = tmp_path / "nw-source-0123456789ab.tar.gz"
    path.write_bytes(b"bundle")
    return Bundle(path=path, sha256_12="0123456789ab", git_sha="abc", files=1)


def _registered(reg, alice, fakes):
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="nw-art-"))
    (tmp / "model.joblib").write_text("m")
    return reg.register(alice, "triage", tmp, {"f1": 0.9}, {})


def test_agents_deploy_invoke_register_status(
    cfg, clients, fakes, alice, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    # deploy() imports google.cloud.aiplatform_v1 for message types; a stub is enough here.
    v1 = types.ModuleType("google.cloud.aiplatform_v1")
    v1.EnvVar = lambda name, value: SimpleNamespace(name=name, value=value)
    monkeypatch.setitem(sys.modules, "google.cloud.aiplatform_v1", v1)
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    cloud.aiplatform_v1 = v1
    google.cloud = cloud
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)

    fakes["reasoning_engines"].engines.append(engine("northwind-alice-agent"))
    rt = gcp.AgentEngineRuntime(cfg, clients)
    name = rt.deploy(alice, "img:2", {"NW_AGENT_ROLE": "resolver"}, version="v7")
    assert name.endswith("/northwind-alice-agent")
    updated, mask = fakes["reasoning_engines"].updates[0]
    assert updated.spec.container_spec.image_uri == "img:2"
    env = {e.name: e.value for e in updated.spec.deployment_spec.env}
    assert (
        env["NW_AGENT_VERSION"] == "v7"
        and env["NW_AGENT_ROLE"] == "resolver"
        and env["NW_TENANT"] == "alice"
    )
    assert mask == {"paths": ["spec.container_spec.image_uri", "spec.deployment_spec.env"]}

    out = rt.invoke(alice, {"task": "refund?", "ticket_id": "T1"}, session_id="s1")
    assert out == {"ticket_id": "T1", "queue": "billing"}
    req = fakes["reasoning_engine_execution"].requests[0]
    assert req["class_method"] == "route" and req["input"]["session_id"] == "s1"

    uri = rt.register(
        alice, {"name": "agent", "owner": "alice", "risk": "medium", "evaluation_tier": "session"}
    )
    # the tenant writes only its own prefix; the platform identity merges the cards
    assert uri == "gs://arts/northwind-alice/agents/northwind-alice-agent.json"
    assert "agents/agents.json" not in fakes["storage"].buckets["arts"]
    assert rt.merge_registry() == "gs://arts/agents/agents.json"
    registry = json.loads(fakes["storage"].buckets["arts"]["agents/agents.json"])
    assert len(registry["agents"]) == 1
    card = registry["agents"][0]
    assert (
        card["id"] == "northwind-alice-agent"
        and card["agent_version"] == "v7"
        and card["risk"] == "medium"
    )
    status = rt.status(alice)
    assert (
        status["state"] == "deployed"
        and status["image"] == "img:2"
        and status["registry"]["owner"] == "alice"
    )
    assert rt.status(Tenant(name="bob"))["state"] == "absent"
    with pytest.raises(KeyError):
        rt.invoke(Tenant(name="bob"), {"task": "x"})
    # a tenant never creates an engine: Terraform does
    with pytest.raises(KeyError, match="Terraform creates"):
        rt.deploy(Tenant(name="bob"), "img:2", {}, version="v1")
