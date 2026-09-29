"""nw.platform.azure against fakes: no credential, no network. Each fake implements the few
calls the implementation makes, so a failure here means the implementation changed what it
asks of Azure ML, Blob Storage, AI Search or the Foundry project. The SDK entity classes
(Model, ManagedOnlineDeployment, HostedAgentDefinition) are the real ones; they are built
locally and never sent."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nw.config import Settings, Track
from nw.platform import azure
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
)

pytest.importorskip("azure.ai.ml")
pytest.importorskip("azure.ai.projects")
pytest.importorskip("azure.search.documents")


class ResourceNotFoundError(Exception):
    """Named like azure.core's, which is all the implementation checks."""


class Poller:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def result(self) -> Any:
        return self.value


# ----- fakes: blob --------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, str], name: str) -> None:
        self.store, self.name = store, name

    def exists(self) -> bool:
        return self.name in self.store

    def download_blob(self) -> Any:
        return SimpleNamespace(readall=lambda: self.store[self.name].encode("utf-8"))

    def upload_blob(self, data: bytes, overwrite: bool = False, **kw: Any) -> None:
        assert overwrite
        self.store[self.name] = data.decode("utf-8")


class FakeBlobService:
    def __init__(self) -> None:
        self.containers: dict[str, dict[str, str]] = {}

    def get_blob_client(self, container: str, blob: str) -> FakeBlob:
        return FakeBlob(self.containers.setdefault(container, {}), blob)


# ----- fakes: Azure ML ----------------------------------------------------------------------


class FakeModels:
    def __init__(self) -> None:
        self.store: dict[str, list[Any]] = {}
        self.writes: list[Any] = []
        self.downloads: list[tuple[str, str, Path]] = []

    def list(self, name: str | None = None) -> list[Any]:
        if name not in self.store:
            raise ResourceNotFoundError(name)
        return [copy.deepcopy(m) for m in self.store[name]]

    def create_or_update(self, model: Any) -> Any:
        self.writes.append(model)
        versions = self.store.setdefault(model.name, [])
        stored = SimpleNamespace(
            name=model.name,
            version=str(model.version),
            tags=dict(model.tags or {}),
            path=getattr(model, "path", None),
            type=getattr(model, "type", None),
            properties=dict(getattr(model, "properties", None) or {}),
        )
        for i, v in enumerate(versions):
            if v.version == stored.version:
                versions[i] = stored
                break
        else:
            versions.append(stored)
        return copy.deepcopy(stored)

    def download(self, name: str, version: str, download_path: Path) -> None:
        self.downloads.append((name, version, Path(download_path)))
        target = Path(download_path) / name / "20260929-120000"
        target.mkdir(parents=True)
        (target / "metadata.json").write_text("{}")


class FakeJobs:
    def __init__(self) -> None:
        self.created: list[tuple[Any, str]] = []
        self.states: list[str] = ["Running", "Completed"]
        self.children = [
            SimpleNamespace(name="c1", display_name="train", status="Completed"),
            SimpleNamespace(name="c2", display_name="register", status="Completed"),
        ]

    def create_or_update(self, job: Any, experiment_name: str | None = None) -> Any:
        self.created.append((job, experiment_name))
        return SimpleNamespace(
            name="happy_turnip_x1",
            status="NotStarted",
            studio_url="https://ml.azure.com/runs/happy_turnip_x1",
        )

    def get(self, name: str) -> Any:
        status = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SimpleNamespace(name=name, status=status, studio_url="https://ml.azure.com/x")

    def list(self, parent_job_name: str | None = None) -> list[Any]:
        return self.children

    def stream(self, name: str) -> None:
        print(f"RunId: {name}")
        print("Execution Summary")


class FakeEndpoints:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.deleted: list[str] = []

    def get(self, name: str) -> Any:
        if name not in self.store:
            raise ResourceNotFoundError(name)
        return copy.deepcopy(self.store[name])

    def begin_create_or_update(self, endpoint: Any) -> Poller:
        stored = SimpleNamespace(
            name=endpoint.name,
            traffic=dict(getattr(endpoint, "traffic", None) or {}),
            scoring_uri=f"https://{endpoint.name}.eastus2.inference.ml.azure.com/score",
            tags=dict(getattr(endpoint, "tags", None) or {}),
            auth_mode=getattr(endpoint, "auth_mode", None),
        )
        self.store[endpoint.name] = stored
        return Poller(copy.deepcopy(stored))

    def get_keys(self, name: str) -> Any:
        return SimpleNamespace(primary_key="endpoint-key")

    def begin_delete(self, name: str) -> Poller:
        self.deleted.append(name)
        self.store.pop(name, None)
        return Poller()


class FakeDeployments:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}
        self.deleted: list[tuple[str, str]] = []

    def begin_create_or_update(self, deployment: Any) -> Poller:
        self.store[(deployment.endpoint_name, deployment.name)] = deployment
        return Poller(deployment)

    def list(self, endpoint_name: str) -> list[Any]:
        return [d for (e, _), d in self.store.items() if e == endpoint_name]

    def begin_delete(self, name: str, endpoint_name: str) -> Poller:
        self.deleted.append((endpoint_name, name))
        self.store.pop((endpoint_name, name), None)
        return Poller()


class FakeData:
    def __init__(self) -> None:
        self.writes: list[Any] = []

    def create_or_update(self, data: Any) -> Any:
        self.writes.append(data)
        return data


class FakeML:
    def __init__(self) -> None:
        self.models = FakeModels()
        self.jobs = FakeJobs()
        self.online_endpoints = FakeEndpoints()
        self.online_deployments = FakeDeployments()
        self.data = FakeData()


# ----- fakes: HTTP, credential, search, Foundry agents --------------------------------------


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload, self.status_code = payload, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self.payload


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None, params: Any = None):
        self.calls.append({"url": url, "json": json, "headers": headers, "params": params})
        if url.endswith("/embeddings"):
            data = [{"index": i, "embedding": [float(i), 0.5]} for i in range(len(json["input"]))]
            return FakeResponse({"data": list(reversed(data))})
        if "/protocols/invocations" in url:
            return FakeResponse({"run_id": "r1", "final": "refund approved", "steps": 2})
        return FakeResponse({"predictions": [{"queue": "billing"}]})


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, scope: str) -> Any:
        self.scopes.append(scope)
        return SimpleNamespace(token="entra-token", expires_on=9e12)


class FakeSearchIndexes:
    def __init__(self) -> None:
        self.indexes: dict[str, Any] = {}
        self.deleted: list[str] = []

    def create_or_update_index(self, index: Any) -> Any:
        self.indexes[index.name] = index
        return index

    def delete_index(self, name: str) -> None:
        self.deleted.append(name)


class FakeSearch:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.queries: list[dict[str, Any]] = []

    def merge_or_upload_documents(self, documents: list[dict[str, Any]]) -> None:
        for d in documents:
            self.docs[d["key"]] = d

    def search(self, **kw: Any) -> list[dict[str, Any]]:
        self.queries.append(kw)
        return [
            {"id": d["id"], "text": d["text"], "metadata": d["metadata"], "@search.score": 0.03}
            for d in list(self.docs.values())[: kw["top"]]
        ]

    def get_document_count(self) -> int:
        return len(self.docs)


class FakeAgents:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.versions: list[dict[str, Any]] = []
        self.statuses = statuses or ["creating", "active"]
        self.created: list[dict[str, Any]] = []

    def create_version(self, agent_name: str, definition: Any, metadata: Any, description: str):
        version = str(len(self.versions) + 1)
        self.created.append(
            {"agent_name": agent_name, "definition": definition, "metadata": metadata}
        )
        self.versions.append(
            {"name": agent_name, "version": version, "status": "creating", "metadata": metadata}
        )
        return SimpleNamespace(name=agent_name, version=version)

    def get_version(self, agent_name: str, agent_version: str) -> dict[str, Any]:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        for v in self.versions:
            if v["version"] == agent_version:
                v["status"] = status
                return dict(v)
        raise ResourceNotFoundError(agent_version)

    def list_versions(self, agent_name: str) -> list[dict[str, Any]]:
        found = [v for v in self.versions if v["name"] == agent_name]
        if not found:
            raise ResourceNotFoundError(agent_name)
        return found


# ----- fixtures --------------------------------------------------------------------------------

OUTPUTS = {
    "NW_AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
    "NW_AZURE_RESOURCE_GROUP": "rg-northwind",
    "NW_AZURE_LOCATION": "eastus2",
    "NW_AZURE_ML_WORKSPACE": "northwind-mlw",
    "NW_AZURE_FOUNDRY_ENDPOINT": "https://northwind-foundry.services.ai.azure.com",
    "NW_AZURE_FOUNDRY_PROJECT": "northwind",
    "NW_AZURE_SEARCH_ENDPOINT": "https://northwind-search.search.windows.net",
    "NW_AZURE_KEY_VAULT": "nwnorthwindkvx7k2q9",
    "NW_AZURE_ACR": "nwnorthwindacrx7k2q9.azurecr.io",
    "NW_AZURE_STORAGE_ACCOUNT": "nwnorthwindstx7k2q9",
    "NW_AZURE_APPINSIGHTS_CONNECTION_STRING": "InstrumentationKey=abc",
    "NW_AZURE_CONTAINERAPPS_ENV": "northwind-cae",
    "NW_AZURE_RAI_POLICY": "northwind-guardrail",
    "NW_AZURE_PIPELINE_IDENTITY_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
}


def _settings() -> Settings:
    return Settings(_env_file=None, track=Track.AZURE)


@pytest.fixture
def cfg() -> azure.AzureConfig:
    return azure.AzureConfig.from_settings(_settings(), env={}, outputs=OUTPUTS)


@pytest.fixture
def fakes() -> SimpleNamespace:
    return SimpleNamespace(
        ml=FakeML(),
        blob=FakeBlobService(),
        http=FakeHttp(),
        credential=FakeCredential(),
        search_index=FakeSearchIndexes(),
        search=FakeSearch(),
        agents=FakeAgents(),
    )


@pytest.fixture
def clients(cfg, fakes) -> azure.AzureClients:
    c = azure.AzureClients(cfg).set(
        ml=fakes.ml,
        blob=fakes.blob,
        http=fakes.http,
        credential=fakes.credential,
        search_index=fakes.search_index,
        projects=SimpleNamespace(agents=fakes.agents),
    )
    c.set(**{"search:northwind-alice-policies": fakes.search})
    return c


@pytest.fixture
def tenant() -> Tenant:
    return Tenant("alice", "northwind")


@pytest.fixture
def platform(cfg, clients) -> azure.AzurePlatform:
    from unittest.mock import patch

    with patch.object(azure.AzureConfig, "from_settings", return_value=cfg):
        return azure.build(_settings(), clients)


def _artifact(tmp_path: Path) -> Path:
    d = tmp_path / "20260929-120000"
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({"version": "20260929-120000"}))
    (d / "model.joblib").write_text("weights")
    return d


# ----- configuration and build -----------------------------------------------------------------


def test_build_returns_every_protocol(platform):
    assert platform.track is Track.AZURE
    assert isinstance(platform.registry, ModelRegistry)
    assert isinstance(platform.pipelines, PipelineRunner)
    assert isinstance(platform.endpoints, EndpointClient)
    assert isinstance(platform.prompts, PromptStore)
    assert isinstance(platform.vectors, VectorStore)
    assert isinstance(platform.agents, AgentRuntime)
    assert platform.describe()["registry"] == "AzureMLRegistry"
    assert platform.describe()["agents"] == "FoundryHostedAgentRuntime"
    assert platform.describe()["gateway"] == "direct"


def test_config_env_wins_over_outputs_and_arm_form_is_read(tmp_path):
    arm = {k: {"type": "String", "value": v} for k, v in OUTPUTS.items()}
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(arm))
    cfg = azure.AzureConfig.from_settings(
        _settings(),
        env={
            "NW_AZURE_ML_WORKSPACE": "override-mlw",
            "NW_AZURE_IMAGE_TRIAGE": "x.azurecr.io/nw-triage:sha",
            "NW_AZURE_APIM_GATEWAY_URL": "https://northwind-apim.azure-api.net",
        },
        outputs=path,
    )
    assert cfg.ml_workspace == "override-mlw"
    assert cfg.search_endpoint == OUTPUTS["NW_AZURE_SEARCH_ENDPOINT"]
    assert cfg.image_for("triage") == "x.azurecr.io/nw-triage:sha"
    assert cfg.image_for("semantic") == "nwnorthwindacrx7k2q9.azurecr.io/nw-semantic:latest"
    assert cfg.gateway_url == "https://northwind-apim.azure-api.net"
    assert cfg.project_endpoint == (
        "https://northwind-foundry.services.ai.azure.com/api/projects/northwind"
    )
    assert cfg.blob_url == "https://nwnorthwindstx7k2q9.blob.core.windows.net"


def test_config_requires_subscription_and_group():
    with pytest.raises(ValueError, match="NW_AZURE_SUBSCRIPTION_ID"):
        azure.AzureConfig.from_settings(_settings(), env={}, outputs={})


def test_describe_prints_names_without_azure(monkeypatch, cfg):
    monkeypatch.setattr(azure.AzureConfig, "from_settings", lambda *a, **k: cfg)
    lines = azure.describe(_settings(), Tenant("alice"))
    text = "\n".join(lines)
    assert "northwind-alice-triage" in text and f"nw-alice-triage-{cfg.scope}" in text
    assert "InstrumentationKey" not in text


# ----- registry --------------------------------------------------------------------------------


def test_register_uploads_a_custom_model_as_a_candidate(platform, fakes, tenant, tmp_path):
    v = platform.registry.register(
        tenant, "triage", _artifact(tmp_path), {"macro_f1": 0.81}, {"git_sha": "abc123"}
    )
    assert (v.version, v.stage, v.uri) == ("1", Stage.CANDIDATE, "azureml:northwind-alice-triage:1")
    written = fakes.ml.models.writes[0]
    assert written.name == "northwind-alice-triage" and written.type == "custom_model"
    assert written.tags["stage"] == "candidate" and written.tags["metric.macro_f1"] == "0.81"
    assert written.tags["tenant"] == "alice" and written.properties["nw.metrics"]
    assert v.metrics == {"macro_f1": 0.81}
    v2 = platform.registry.register(tenant, "triage", _artifact(tmp_path / "b"), {}, {})
    assert v2.version == "2"
    trail = fakes.blob.containers["artifacts"]["northwind-alice/registry/triage/stages.jsonl"]
    assert len(trail.splitlines()) == 2


def test_stage_moves_and_the_previous_live_retires(platform, fakes, tenant, tmp_path):
    for sub in ("a", "b"):
        platform.registry.register(tenant, "triage", _artifact(tmp_path / sub), {}, {})
    platform.registry.set_stage(tenant, "triage", "1", Stage.LIVE, "first release")
    assert platform.registry.live(tenant, "triage").version == "1"
    out = platform.registry.set_stage(tenant, "triage", "2", Stage.LIVE, "canary passed")
    assert out.stage == Stage.LIVE and out.tags["stage_reason"] == "canary passed"
    stages = {v.version: v.stage for v in platform.registry.versions(tenant, "triage")}
    assert stages == {"1": Stage.RETIRED, "2": Stage.LIVE}
    with pytest.raises(KeyError):
        platform.registry.set_stage(tenant, "triage", "9", Stage.APPROVED, "no such")


def test_versions_of_an_unknown_model_is_empty(platform, tenant):
    assert platform.registry.versions(tenant, "semantic") == []
    assert platform.registry.live(tenant, "semantic") is None


def test_download_finds_the_artifact_directory(platform, fakes, tenant, tmp_path):
    v = platform.registry.register(tenant, "triage", _artifact(tmp_path / "src"), {}, {})
    got = platform.registry.download(tenant, v, tmp_path / "out")
    assert (got / "metadata.json").exists()
    assert fakes.ml.models.downloads[0][:2] == ("northwind-alice-triage", "1")


def test_registry_from_env_builds_from_the_step_environment(monkeypatch):
    for k, v in OUTPUTS.items():
        monkeypatch.setenv(k, v)
    registry = azure.registry_from_env()
    assert isinstance(registry, azure.AzureMLRegistry)
    assert registry.cfg.ml_workspace == "northwind-mlw"


# ----- pipelines -------------------------------------------------------------------------------


def test_submit_builds_and_sends_the_job(platform, fakes, tenant):
    run = platform.pipelines.submit(tenant, "retrain-triage", {"trigger": "schedule"})
    assert run.run_id == "happy_turnip_x1" and run.status == RunStatus.QUEUED
    assert run.url.startswith("https://ml.azure.com/")
    job, experiment = fakes.ml.jobs.created[0]
    assert experiment == "northwind-alice-triage"
    d = job._to_dict()
    assert d["inputs"]["trigger"] == "schedule"
    assert d["inputs"]["data_uri"]["path"] == (
        "azureml://datastores/workspaceblobstore/paths/tickets/tickets.jsonl"
    )
    register = d["jobs"]["register"]
    assert register["environment"]["image"] == (
        "nwnorthwindacrx7k2q9.azurecr.io/nw-pipelines:latest"
    )
    env = register["environment_variables"]
    assert env["NW_AZURE_ML_WORKSPACE"] == "northwind-mlw"
    assert env["AZURE_CLIENT_ID"] == OUTPUTS["NW_AZURE_PIPELINE_IDENTITY_CLIENT_ID"]


def test_status_wait_and_logs(platform, fakes, tenant):
    sleeps: list[float] = []
    platform.pipelines.sleep = sleeps.append
    run = platform.pipelines.submit(tenant, "semantic", {})
    done = platform.pipelines.wait(tenant, run, timeout_s=60)
    assert done.status == RunStatus.SUCCEEDED and sleeps == [30.0]
    assert done.outputs == {"train": "Completed", "register": "Completed"}
    assert list(platform.pipelines.logs(tenant, run)) == [
        "RunId: happy_turnip_x1",
        "Execution Summary",
    ]


def test_image_override_and_missing_image(cfg, clients, tenant):
    runner = azure.AzureMLPipelineRunner(cfg, clients)
    job = runner.job(tenant, "triage", {"image_uri": "other.azurecr.io/nw-pipelines:sha"})
    assert job._to_dict()["jobs"]["train"]["environment"]["image"] == (
        "other.azurecr.io/nw-pipelines:sha"
    )
    cfg.acr = ""
    with pytest.raises(ValueError, match="NW_AZURE_IMAGE_PIPELINES"):
        runner.job(tenant, "triage", {})


# ----- endpoints -------------------------------------------------------------------------------


def _version(n: str = "3") -> Any:
    from nw.platform.base import ModelVersion

    return ModelVersion(name="triage", version=n, stage=Stage.APPROVED, uri="azureml:x:3")


def test_tenant_deploy_creates_the_endpoint_and_a_blue_deployment(platform, fakes, tenant, cfg):
    uri = platform.endpoints.deploy(tenant, _version())
    name = f"nw-alice-triage-{cfg.scope}"
    assert uri == f"https://{name}.eastus2.inference.ml.azure.com/score"
    endpoint = fakes.ml.online_endpoints.store[name]
    assert endpoint.traffic == {"blue": 100} and endpoint.auth_mode == "key"
    d = fakes.ml.online_deployments.store[(name, "blue")]
    assert d.model == "azureml:northwind-alice-triage:3"
    assert d.instance_type == "Standard_F2s_v2" and d.instance_count == 1
    assert d.environment.image == "nwnorthwindacrx7k2q9.azurecr.io/nw-triage:latest"
    assert d.environment.inference_config["scoring_route"] == {"port": 8080, "path": "/predict"}
    assert d.model_mount_path == "/var/nw/model"
    assert d.environment_variables["NW_MODEL_URI"] == "file:///var/nw/model"
    assert d.environment_variables["AIP_HTTP_PORT"] == "8080"
    assert d.environment_variables["NW_MODEL_VERSION"] == "3"
    # a second deploy replaces blue in place
    platform.endpoints.deploy(tenant, _version("4"))
    assert list(fakes.ml.online_deployments.store) == [(name, "blue")]


def test_live_canary_uses_the_other_colour_then_promotes(platform, fakes, cfg):
    live = Tenant("live", "northwind")
    name = f"nw-live-triage-{cfg.scope}"
    platform.endpoints.deploy(live, _version("1"), live=True)
    assert fakes.ml.online_endpoints.store[name].traffic == {"blue": 100}
    platform.endpoints.deploy(live, _version("2"), live=True, canary_percent=10)
    assert fakes.ml.online_endpoints.store[name].traffic == {"green": 10, "blue": 90}
    green = fakes.ml.online_deployments.store[(name, "green")]
    assert green.instance_type == "Standard_DS3_v2" and green.tags["model_version"] == "2"
    status = platform.endpoints.status(live, "triage")
    assert status["traffic"] == {"green": 10, "blue": 90} and len(status["deployments"]) == 2
    assert platform.endpoints.promote(live, "triage") == "green"
    assert fakes.ml.online_endpoints.store[name].traffic == {"green": 100, "blue": 0}
    assert fakes.ml.online_deployments.deleted == [(name, "blue")]


def test_live_rollback_removes_the_canary(platform, fakes, cfg):
    live = Tenant("live", "northwind")
    name = f"nw-live-triage-{cfg.scope}"
    platform.endpoints.deploy(live, _version("1"), live=True)
    platform.endpoints.deploy(live, _version("2"), live=True, canary_percent=25)
    assert platform.endpoints.rollback(live, "triage") == "blue"
    assert fakes.ml.online_endpoints.store[name].traffic == {"blue": 100, "green": 0}
    assert fakes.ml.online_deployments.deleted == [(name, "green")]


def test_live_deployments_collect_inputs_and_outputs_for_monitoring(platform, fakes, tenant, cfg):
    from azure.ai.ml.entities import DataCollector

    live = Tenant("live", "northwind")
    platform.endpoints.deploy(live, _version("1"), live=True)
    d = fakes.ml.online_deployments.store[(f"nw-live-triage-{cfg.scope}", "blue")]
    assert isinstance(d.data_collector, DataCollector)
    collections = d.data_collector.collections
    assert {"model_inputs", "model_outputs"} <= set(collections)
    assert {"request", "response"} <= set(collections), "payload logging for the custom image"
    assert all(c.enabled == "true" for c in collections.values())
    assert d.data_collector.sampling_rate == 1.0 and d.data_collector.rolling_rate == "hour"
    # The REST shape the SDK sends carries the collections, so the deployment asks for them.
    rest = d.data_collector._to_rest_object()
    assert {"model_inputs", "model_outputs"} <= set(rest.collections)
    # A tenant's deployment collects nothing unless the environment opts in.
    platform.endpoints.deploy(tenant, _version())
    t = fakes.ml.online_deployments.store[(f"nw-alice-triage-{cfg.scope}", "blue")]
    assert t.data_collector is None


def test_tenant_data_collection_is_opt_in(fakes, clients, tenant):
    opted = azure.AzureConfig.from_settings(
        _settings(),
        env={"NW_AZURE_TENANT_DATA_COLLECTION": "1", "NW_AZURE_DATA_SAMPLING_RATE": "0.25"},
        outputs=OUTPUTS,
    )
    assert opted.tenant_data_collection and opted.data_sampling_rate == 0.25
    endpoints = azure.AzureMLEndpointClient(opted, clients)
    endpoints.deploy(tenant, _version())
    d = fakes.ml.online_deployments.store[(f"nw-alice-triage-{opted.scope}", "blue")]
    assert set(d.data_collector.collections) >= {"model_inputs", "model_outputs"}
    assert d.data_collector.sampling_rate == 0.25


def test_invoke_posts_instances_with_the_endpoint_key(platform, fakes, tenant, cfg):
    platform.endpoints.deploy(tenant, _version())
    out = platform.endpoints.invoke(tenant, "triage", {"subject": "refund", "path": "/triage"})
    assert out == {"predictions": [{"queue": "billing"}]}
    call = fakes.http.calls[-1]
    assert call["url"].endswith("/score")
    assert call["json"] == {"instances": [{"subject": "refund"}]}
    assert call["headers"]["authorization"] == "Bearer endpoint-key"
    platform.endpoints.invoke(tenant, "triage", {"instances": [{"a": 1}], "deployment": "green"})
    assert fakes.http.calls[-1]["headers"]["azureml-model-deployment"] == "green"


def test_status_of_a_missing_endpoint_and_delete(platform, fakes, tenant, cfg):
    assert platform.endpoints.status(tenant, "semantic")["state"] == "absent"
    platform.endpoints.deploy(tenant, _version())
    platform.endpoints.delete(tenant, "triage")
    assert fakes.ml.online_endpoints.deleted == [f"nw-alice-triage-{cfg.scope}"]


def test_live_delete_keeps_the_endpoint(platform, fakes, cfg):
    live = Tenant("live", "northwind")
    platform.endpoints.deploy(live, _version("1"), live=True)
    platform.endpoints.delete(live, "triage")
    name = f"nw-live-triage-{cfg.scope}"
    assert name in fakes.ml.online_endpoints.store
    assert fakes.ml.online_endpoints.store[name].traffic == {}
    assert fakes.ml.online_deployments.deleted == [(name, "blue")]


def test_canary_percent_is_bounded(platform, tenant):
    with pytest.raises(ValueError):
        platform.endpoints.deploy(tenant, _version(), canary_percent=120)


# ----- prompts ---------------------------------------------------------------------------------


def test_prompt_versions_are_blobs_and_data_assets(platform, fakes, tenant):
    from nw.llm.prompts import prompt_hash

    text = "Answer from the policy excerpts only."
    v = platform.prompts.register(tenant, "policy.answer", text, {"owner": "policy"})
    assert v.version == v.sha256_12 == prompt_hash(text) and v.stage == Stage.CANDIDATE
    blobs = fakes.blob.containers["artifacts"]
    assert blobs[f"northwind-alice/prompts/policy.answer/{v.version}.txt"] == text
    asset = fakes.ml.data.writes[-1]
    assert asset.name == "northwind-alice-prompt-policy.answer" and asset.version == v.version
    assert asset.type == "uri_file" and asset.tags["sha256_12"] == v.version
    assert asset.path.endswith(f"/artifacts/northwind-alice/prompts/policy.answer/{v.version}.txt")
    # the same text is the same version, registered once
    assert platform.prompts.register(tenant, "policy.answer", text, {}).version == v.version
    assert len(fakes.ml.data.writes) == 1


def test_prompt_stages_step_back_and_get_prefers_live(platform, fakes, tenant):
    a = platform.prompts.register(tenant, "policy.answer", "one", {})
    b = platform.prompts.register(tenant, "policy.answer", "two", {})
    platform.prompts.set_stage(tenant, "policy.answer", a.version, Stage.LIVE)
    assert platform.prompts.get(tenant, "policy.answer").version == a.version
    platform.prompts.set_stage(tenant, "policy.answer", b.version, Stage.LIVE)
    stages = {p.version: p.stage for p in platform.prompts.versions(tenant, "policy.answer")}
    assert stages == {a.version: Stage.RETIRED, b.version: Stage.LIVE}
    assert platform.prompts.get(tenant, "policy.answer").text == "two"
    assert fakes.ml.data.writes[-1].tags["stage"] in ("live", "retired")
    with pytest.raises(KeyError):
        platform.prompts.get(tenant, "missing")


# ----- vectors ---------------------------------------------------------------------------------


def test_upsert_creates_a_hybrid_index_and_embeds_through_foundry(platform, fakes, tenant, cfg):
    n = platform.vectors.upsert(
        tenant,
        "policies",
        ["refunds.md:0", "refunds.md:1"],
        ["Refunds within 30 days.", "Enterprise plans differ."],
        None,
        [{"source": "refunds.md"}, {"source": "refunds.md"}],
    )
    assert n == 2
    index = fakes.search_index.indexes["northwind-alice-policies"]
    fields = {f.name: f for f in index.fields}
    assert fields["key"].key and fields["vector"].vector_search_dimensions == 1536
    vectorizer = index.vector_search.vectorizers[0]
    assert vectorizer.parameters.resource_url == "https://northwind-foundry.services.ai.azure.com"
    assert vectorizer.parameters.deployment_name == "text-embedding-3-small"
    embed = fakes.http.calls[0]
    assert embed["url"] == ("https://northwind-foundry.services.ai.azure.com/openai/v1/embeddings")
    assert embed["headers"]["authorization"] == "Bearer entra-token"
    assert fakes.credential.scopes == ["https://ai.azure.com/.default"]
    stored = list(fakes.search.docs.values())
    assert [d["id"] for d in stored] == ["refunds.md:0", "refunds.md:1"]
    assert stored[1]["vector"] == [1.0, 0.5]  # embeddings reordered by index
    assert json.loads(stored[0]["metadata"]) == {"source": "refunds.md"}


def test_search_is_hybrid_and_uses_integrated_vectorization(platform, fakes, tenant):
    from azure.search.documents.models import VectorizableTextQuery, VectorizedQuery

    platform.vectors.upsert(tenant, "policies", ["a"], ["text a"], [[0.1, 0.2]], [{"k": 1}])
    assert not fakes.http.calls  # vectors given: nothing embedded
    hits = platform.vectors.search(tenant, "policies", "refund window", k=3)
    q = fakes.search.queries[-1]
    assert q["search_text"] == "refund window" and q["top"] == 3
    assert isinstance(q["vector_queries"][0], VectorizableTextQuery)
    assert hits[0].id == "a" and hits[0].metadata == {"k": 1} and hits[0].score == 0.03
    platform.vectors.search(tenant, "policies", "q", vector=[0.3, 0.4])
    assert isinstance(fakes.search.queries[-1]["vector_queries"][0], VectorizedQuery)
    assert platform.vectors.count(tenant, "policies") == 1
    platform.vectors.drop(tenant, "policies")
    assert fakes.search_index.deleted == ["northwind-alice-policies"]


# ----- agents ----------------------------------------------------------------------------------


def test_deploy_creates_a_hosted_agent_version_with_the_guardrail(platform, fakes, tenant):
    sleeps: list[float] = []
    platform.agents.sleep = sleeps.append
    resource = platform.agents.deploy(
        tenant, "nwacr.azurecr.io/nw-agent:sha", {"NW_TRACK": "local", "X": 1}, version="a1b2"
    )
    assert resource == (
        "https://northwind-foundry.services.ai.azure.com/api/projects/northwind"
        "/agents/northwind-alice-agent/versions/1"
    )
    assert sleeps == [5.0]
    created = fakes.agents.created[0]
    assert created["agent_name"] == "northwind-alice-agent"
    assert created["metadata"] == {
        "agent_version": "a1b2",
        "tenant": "alice",
        "image": "nwacr.azurecr.io/nw-agent:sha",
    }
    d = created["definition"]
    assert d.container_configuration.image == "nwacr.azurecr.io/nw-agent:sha"
    assert d.protocol_versions[0].protocol == "invocations"
    assert (d.cpu, d.memory) == ("1", "2Gi")
    env = d.environment_variables
    assert env["NW_TRACK"] == "azure" and env["PORT"] == "8088" and env["X"] == "1"
    assert env["NW_AGENT_VERSION"] == "a1b2" and env["NW_TENANT"] == "alice"
    assert d.rai_config.rai_policy_name == "northwind-guardrail"
    assert d.rai_config.invocations_moderation.input_paths == ["$.task"]
    registry = json.loads(fakes.blob.containers["artifacts"]["agents/agents.json"])
    assert registry["agents"][0]["agent_version"] == "a1b2"
    assert registry["agents"][0]["status"] == "active"


def test_a_failed_version_raises(platform, fakes, tenant):
    fakes.agents.statuses = ["failed"]
    with pytest.raises(RuntimeError, match="failed"):
        platform.agents.deploy(tenant, "img", {}, version="v")


def test_invoke_posts_to_the_invocations_endpoint_with_the_session(platform, fakes, tenant):
    out = platform.agents.invoke(tenant, {"task": "refund?"}, session_id="ticket-42")
    assert out["final"] == "refund approved"
    call = fakes.http.calls[-1]
    assert call["url"] == (
        "https://northwind-foundry.services.ai.azure.com/api/projects/northwind"
        "/agents/northwind-alice-agent/endpoint/protocols/invocations"
    )
    assert call["params"] == {"api-version": "v1", "agent_session_id": "ticket-42"}
    assert call["headers"]["authorization"] == "Bearer entra-token"
    assert call["json"] == {"task": "refund?"}


def test_live_agent_is_container_apps_and_registered_as_such(platform, fakes):
    live = Tenant("live", "northwind")
    with pytest.raises(ValueError, match="Container Apps"):
        platform.agents.deploy(live, "img:1", {}, version="v1")
    assert fakes.agents.created == [], "no hosted agent for the live owner"
    platform.agents.register(live, {"id": "northwind-live-agent", "agent_version": "v2"})
    registry = json.loads(fakes.blob.containers["artifacts"]["agents/agents.json"])
    entry = next(a for a in registry["agents"] if a["tenant"] == "live")
    assert entry["runtime"] == "container-apps" and entry["app"] == "northwind-live-agent"
    assert entry["agent_version"] == "v2"


def test_register_and_status(platform, fakes, tenant):
    assert platform.agents.status(tenant)["state"] == "absent"
    platform.agents.sleep = lambda s: None
    platform.agents.deploy(tenant, "img:1", {}, version="v1")
    platform.agents.register(tenant, {"id": "northwind-alice-agent", "tools": ["triage"]})
    status = platform.agents.status(tenant)
    assert status["state"] == "active" and status["agent_version"] == "v1"
    assert status["registry"]["tools"] == ["triage"]
    assert status["registry"]["runtime"] == "foundry-hosted-agent"
    assert status["registry"]["agent_version"] == "v1"  # the deploy's fields survive


def test_deploy_gives_the_agent_the_apim_route_not_a_litellm_url(platform, fakes, tenant):
    platform.agents.sleep = lambda s: None
    platform.agents.cfg.apim_gateway_url = "https://northwind-apim.azure-api.net"
    platform.agents.deploy(
        tenant, "img:1", {"NW_GATEWAY_URL": "", "NW_GATEWAY_KEY": "sub"}, version="v1"
    )
    env = fakes.agents.created[-1]["definition"].environment_variables
    assert env["NW_AZURE_APIM_GATEWAY_URL"] == "https://northwind-apim.azure-api.net"
    assert env["NW_AZURE_FOUNDRY_ENDPOINT"] == "https://northwind-foundry.services.ai.azure.com"
    assert env["NW_GATEWAY_KEY"] == "sub" and "NW_GATEWAY_URL" not in env
