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


class FakeBlob:
    def __init__(self, store: dict[str, str], name: str) -> None:
        self.store, self.name = store, name

    def exists(self) -> bool:
        return self.name in self.store

    def download_as_text(self) -> str:
        return self.store[self.name]

    def upload_from_string(self, text: str, content_type: str = "") -> None:
        self.store[self.name] = text

    def upload_from_filename(self, path: str) -> None:
        self.store[self.name] = Path(path).read_text()

    def download_to_filename(self, path: str) -> None:
        Path(path).write_text(self.store[self.name])


class FakeBucket:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, name)

    def list_blobs(self, prefix: str = "") -> list[FakeBlob]:
        return [FakeBlob(self.store, k) for k in sorted(self.store) if k.startswith(prefix)]


class FakeStorage:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, str]] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.buckets.setdefault(name, {}))


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


class FakeEndpoint:
    def __init__(self, resource_name: str) -> None:
        self.resource_name = resource_name
        self.traffic_split = {"m1": 100}
        self.undeployed = False

    def list_models(self) -> list[Any]:
        return [SimpleNamespace(id="m1", display_name="northwind-live-triage-v1", model="models/1")]

    def predict(self, instances: list[Any]) -> Any:
        return SimpleNamespace(
            predictions=[{"queue": "billing"} for _ in instances], deployed_model_id="m1"
        )

    def undeploy_all(self) -> None:
        self.undeployed = True


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
    assert [v.stage for v in reg.versions(alice, "triage")] == [Stage.LIVE, Stage.CANDIDATE]
    trail = (
        fakes["storage"]
        .buckets["arts"]["northwind-alice/registry/triage/stages.jsonl"]
        .splitlines()
    )
    assert [json.loads(r)["reason"] for r in trail] == [
        "registered",
        "registered",
        "gate passed",
        "rollback",
    ]

    into = reg.download(alice, v1, tmp_path / "dl")
    assert (into / "model.joblib").read_text() == "bytes" and (into / "MODEL_CARD.md").exists()
    with pytest.raises(KeyError):
        reg.set_stage(alice, "nothing", "1", Stage.APPROVED, "x")


def test_pipelines_submit_status_wait_logs(cfg, clients, fakes, alice, tmp_path: Path) -> None:
    template = tmp_path / "retrain-triage.yaml"
    template.write_text("pipelineInfo: {}")
    sleeps: list[float] = []
    runner = gcp.VertexPipelineRunner(cfg, clients, sleep=sleeps.append)
    with pytest.raises(FileNotFoundError):
        runner.submit(alice, "retrain-triage", {})
    run = runner.submit(alice, "retrain-triage", {"template_path": str(template), "epochs": 2})
    job = FakePipelineJob.jobs[run.run_id]
    assert job.kw["display_name"] == "northwind-alice-retrain-triage"
    assert job.kw["pipeline_root"] == "gs://pipes/northwind-alice"
    assert job.kw["parameter_values"] == {
        "epochs": 2,
        "tenant": "alice",
        "environment": "northwind",
        "output_root": "gs://arts/northwind-alice/pipelines/runs",
        "production_summary": "gs://arts/baselines/triage_production.json",
    }
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
    runner = gcp.VertexPipelineRunner(cfg, clients, sleep=lambda s: None)
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
    assert (
        model.deployed[0]["traffic_percentage"] == 10
        and model.deployed[0]["endpoint"].resource_name == name
    )
    assert ep.invoke(live, "triage", {"instances": [{"a": 1}]})["predictions"] == [
        {"queue": "billing"}
    ]
    assert ep.status(live, "triage")["traffic_split"] == {"m1": 100}
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
        ["refund-policy", "sla"],
        ["Refunds within 30 days.", "SLA is 99.9."],
        None,
        [{"vintage": "2025"}, {}],
    )
    assert n == 2
    imp = fakes["rag"].imports[0]
    assert imp["corpus"].endswith("/7") and imp["paths"] == [
        "gs://arts/northwind-alice/rag/policies/"
    ]
    assert (
        fakes["storage"].buckets["arts"]["northwind-alice/rag/policies/refund-policy.txt"]
        == "Refunds within 30 days."
    )
    assert (
        json.loads(
            fakes["storage"].buckets["arts"]["northwind-alice/rag/policies/refund-policy.meta.json"]
        )["vintage"]
        == "2025"
    )
    hits = vs.search(alice, "policies", "refund?", k=3)
    assert (
        hits[0].id == "refund-policy"
        and hits[0].score == 0.91
        and fakes["rag"].queries[0]["top_k"] == 3
    )
    assert vs.count(alice, "policies") == 1
    vs.drop(alice, "policies")
    assert vs.count(alice, "policies") == 0
    with pytest.raises(KeyError):
        vs.search(Tenant(name="bob"), "policies", "x")


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
    assert uri == "gs://arts/agents/agents.json"
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
