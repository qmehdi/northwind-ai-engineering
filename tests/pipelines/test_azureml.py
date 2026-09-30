"""The Azure ML definitions: built locally with the SDK, the job carries the command steps in
order, each running the shared step module in the course image, parameters generated from
`nw.pipelines.params`, a register step that uses the Azure registry, and never calls Azure."""

from __future__ import annotations

import pytest

from nw.pipelines.params import BY_PIPELINE, defaults
from nw.platform.base import Tenant

pytestmark = pytest.mark.pipelines
pytest.importorskip("azure.ai.ml")

from nw.pipelines.azureml import AzureMLConfig, as_dict, definition  # noqa: E402
from nw.pipelines.azureml.definitions import REGISTRY_FACTORY  # noqa: E402

IMAGE = "nwnorthwindacr.azurecr.io/nw-pipelines:test"


@pytest.fixture(scope="module")
def config() -> AzureMLConfig:
    return AzureMLConfig(
        tenant=Tenant("alice", "northwind"),
        image=IMAGE,
        identity_client_id="11111111-2222-3333-4444-555555555555",
        platform_env={"NW_AZURE_SUBSCRIPTION_ID": "sub", "NW_AZURE_ML_WORKSPACE": "ws"},
        defaults={
            "data_uri": "azureml://datastores/workspaceblobstore/paths/tickets/tickets.jsonl",
            "production_summary": "azureml://datastores/workspaceblobstore/paths/baselines/"
            "triage_production.json",
            "output_root": "azureml://datastores/workspaceblobstore/paths/northwind-alice/runs",
        },
    )


@pytest.fixture(scope="module")
def triage(config):
    return as_dict(definition("triage", config))


@pytest.fixture(scope="module")
def semantic(config):
    return as_dict(definition("semantic", config))


def _commands(d: dict) -> dict[str, str]:
    return {name: job["component"]["command"] for name, job in d["jobs"].items()}


def test_triage_steps_in_order_with_the_shared_modules(triage):
    commands = _commands(triage)
    assert list(commands) == ["data_check", "train", "evaluate", "register"]
    assert "nw.pipelines.steps.triage_data_check" in commands["data_check"]
    assert "nw.pipelines.steps.triage_train" in commands["train"]
    assert "nw.pipelines.steps.triage_evaluate" in commands["evaluate"]
    assert "nw.pipelines.steps.register --pipeline triage" in commands["register"]
    # every step after the first starts from its predecessor's artifact tree
    assert not commands["data_check"].startswith("cp -R")
    for name in ("train", "evaluate", "register"):
        assert commands[name].startswith("cp -R ${{inputs.tree}}/. ${{outputs.artifacts}}/ && ")
    chain = {n: triage["jobs"][n]["inputs"].get("tree", {}).get("path") for n in commands}
    assert chain["train"] == "${{parent.jobs.data_check.outputs.artifacts}}"
    assert chain["evaluate"] == "${{parent.jobs.train.outputs.artifacts}}"
    assert chain["register"] == "${{parent.jobs.evaluate.outputs.artifacts}}"


def test_semantic_steps_in_order(semantic):
    commands = _commands(semantic)
    assert list(commands) == ["data_prep", "train", "export", "benchmark", "gate", "register"]
    assert "--n ${{inputs.parity_n}}" in commands["export"]
    assert "nw.pipelines.steps.semantic_gate" in commands["gate"]
    # the training step gets the bigger machine
    assert semantic["jobs"]["train"]["resources"]["instance_type"] == "Standard_D8s_v3"
    assert semantic["jobs"]["gate"]["resources"]["instance_type"] == "Standard_DS3_v2"


def test_optional_flags_only_when_set(config):
    unset = _commands(as_dict(definition("semantic", config)))
    assert "$[[--base ${{inputs.base}}]]" in unset["train"]
    assert "$[[--triage ${{inputs.triage_artifact}}]]" in unset["benchmark"]
    job = as_dict(definition("semantic", config))
    assert "base" not in job["jobs"]["train"]["inputs"]
    assert "triage_artifact" not in job["jobs"]["benchmark"]["inputs"]
    set_ = as_dict(
        definition("semantic", config, {"base": "tiny", "triage_artifact": "azureml:t:1"})
    )
    assert set_["jobs"]["train"]["inputs"]["base"]["path"] == "${{parent.inputs.base}}"
    assert set_["jobs"]["benchmark"]["inputs"]["triage_artifact"]["path"] == (
        "${{parent.inputs.triage_artifact}}"
    )


@pytest.mark.parametrize("pipeline", ["triage", "semantic"])
def test_pipeline_inputs_are_the_shared_parameters(config, pipeline):
    d = as_dict(definition(pipeline, config))
    # the bundle is an optional file input: absent until the platform client sets it
    assert set(d["inputs"]) == {p.name for p in BY_PIPELINE[pipeline]} - {"source_uri"}
    for name, value in defaults(pipeline).items():
        if name in ("data_uri", "production_summary", "output_root", "tenant", "source_uri"):
            continue
        got = d["inputs"][name]
        assert got == value or float(got) == float(value), name


def test_deployed_defaults_and_overrides(config):
    d = as_dict(definition("triage", config, {"min_p0_recall": 0.9, "trigger": "schedule"}))
    assert d["inputs"]["data_uri"]["type"] == "uri_file"
    assert d["inputs"]["data_uri"]["path"].endswith("paths/tickets/tickets.jsonl")
    assert d["inputs"]["production_summary"]["path"].endswith("baselines/triage_production.json")
    assert d["inputs"]["min_p0_recall"] == 0.9
    assert d["inputs"]["trigger"] == "schedule"
    assert d["inputs"]["tenant"] == "alice"
    assert d["outputs"]["artifacts"]["path"] == (
        "azureml://datastores/workspaceblobstore/paths/northwind-alice/runs/triage/${{name}}/"
    )


def test_job_metadata_compute_and_identity(triage, config):
    assert triage["experiment_name"] == "northwind-alice-triage"
    assert triage["settings"]["default_compute"] == "serverless"
    assert triage["tags"] == {
        "tenant": "alice",
        "environment": "northwind",
        "pipeline": "triage",
        "trigger": "manual",
    }
    assert triage["identity"]["client_id"] == config.identity_client_id
    for job in triage["jobs"].values():
        assert job["environment"]["image"] == IMAGE
        env = job["environment_variables"]
        assert env["NW_TRACK"] == "azure" and env["NW_TENANT"] == "alice"
        assert env["AZURE_CLIENT_ID"] == config.identity_client_id
        assert job["component"]["is_deterministic"] is False


def test_register_step_uses_the_azure_registry_and_honours_register_model(triage):
    job = triage["jobs"]["register"]
    env = job["environment_variables"]
    assert env["NW_PIPELINE_REGISTRY"] == REGISTRY_FACTORY == "nw.platform.azure:registry_from_env"
    assert env["NW_AZURE_SUBSCRIPTION_ID"] == "sub" and env["NW_AZURE_ML_WORKSPACE"] == "ws"
    command = job["component"]["command"]
    assert 'case "${{inputs.register_model}}" in [Tt]rue)' in command
    assert "--tenant alice --environment northwind --trigger ${{inputs.trigger}}" in command
    # the gate flags reach the evaluate step
    evaluate = triage["jobs"]["evaluate"]["component"]["command"]
    for flag in ("--min-p0-recall", "--max-ece", "--force", "--production-summary"):
        assert flag in evaluate


def test_every_step_runs_the_launcher_on_the_submitted_bundle(config, tmp_path):
    bundle = tmp_path / "nw-source-abc.tar.gz"
    bundle.write_bytes(b"x")
    d = as_dict(definition("triage", config, {"source_uri": str(bundle)}))
    assert d["inputs"]["source_uri"]["type"] == "uri_file"
    assert d["inputs"]["source_uri"]["path"].endswith(str(bundle)), "uploaded with the job"
    for name, job in d["jobs"].items():
        command = job["component"]["command"]
        assert "python -m nw.pipelines.source run $[[--source ${{inputs.source}}]] --" in command
        assert job["inputs"]["source"]["path"] == "${{parent.inputs.source_uri}}", name
    evaluate = d["jobs"]["evaluate"]
    assert "--champion ${{inputs.champion}}" in evaluate["component"]["command"]
    assert "--tenant alice" in evaluate["component"]["command"]
    assert evaluate["environment_variables"]["NW_PIPELINE_REGISTRY"] == REGISTRY_FACTORY
    without = as_dict(definition("triage", config))
    assert "source" not in without["jobs"]["train"]["inputs"]


def test_aliases_build_the_same_graph(config):
    a = as_dict(definition("retrain-triage", config))
    b = as_dict(definition("triage", config))
    assert _commands(a) == _commands(b)
    with pytest.raises(ValueError):
        definition("nope", config)


def test_building_makes_no_azure_call(monkeypatch, config):
    """No credential, no MLClient: building a job never authenticates."""
    import azure.identity

    def boom(*a, **k):
        raise AssertionError("a definition must not authenticate")

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", boom)
    job = definition("semantic", config)
    rest = job._to_rest_object()
    assert rest.properties.experiment_name == "northwind-alice-semantic"
