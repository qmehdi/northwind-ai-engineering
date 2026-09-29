"""The Kubeflow definitions: they compile to the YAML Vertex and the local runner accept,
they carry the declared parameters and the course image, and the triage pipeline runs end
to end on the local SubprocessRunner against the fixture."""

from __future__ import annotations

import inspect
import json

import pytest
import yaml

from nw.pipelines.params import BY_PIPELINE, defaults

pytestmark = pytest.mark.pipelines
kfp = pytest.importorskip("kfp")

IMAGE = "registry.example.com/nw-pipelines:test"


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    from nw.pipelines.kfp import compile_all

    out = tmp_path_factory.mktemp("compiled")
    paths = compile_all(out, IMAGE)
    return {name: yaml.safe_load(path.read_text()) for name, path in paths.items()}


def test_compile_writes_the_scheduler_names_too(tmp_path):
    from nw.pipelines.kfp import compile_all

    paths = compile_all(tmp_path, IMAGE)
    for name, alias in (("triage", "retrain-triage"), ("semantic", "retrain-semantic")):
        assert (tmp_path / f"{alias}.yaml").read_bytes() == paths[name].read_bytes()


def test_compile_writes_both_pipelines_with_the_image_and_steps(compiled):
    assert set(compiled) == {"triage", "semantic"}
    triage = compiled["triage"]
    assert triage["pipelineInfo"]["name"] == "northwind-triage"
    assert set(triage["components"]) == {
        "comp-triage-data-check",
        "comp-triage-train",
        "comp-triage-evaluate",
        "comp-register",
    }
    semantic = compiled["semantic"]
    assert set(semantic["components"]) == {
        "comp-semantic-data-prep",
        "comp-semantic-train",
        "comp-semantic-export",
        "comp-semantic-benchmark",
        "comp-semantic-gate",
        "comp-register",
    }
    for spec in compiled.values():
        for executor in spec["deploymentSpec"]["executors"].values():
            assert executor["container"]["image"] == IMAGE
            command = " ".join(executor["container"]["command"])
            assert "nw.pipelines.steps." in command, "every component runs a step module"


def test_pipeline_parameters_match_the_declared_schema(compiled):
    for name, spec in compiled.items():
        declared = spec["root"]["inputDefinitions"]["parameters"]
        assert set(declared) == {p.name for p in BY_PIPELINE[name]}
        for p in BY_PIPELINE[name]:
            assert declared[p.name]["defaultValue"] == p.default, p.name
        assert "Output" in spec["root"]["outputDefinitions"]["parameters"]


def test_pipeline_signatures_repeat_the_schema():
    from nw.pipelines.kfp.pipelines import FACTORIES

    for name, factory in FACTORIES.items():
        signature = inspect.signature(factory(IMAGE).pipeline_func)
        assert {k: v.default for k, v in signature.parameters.items()} == defaults(name)


def test_tasks_are_never_cached_and_the_gate_feeds_register(compiled):
    triage = compiled["triage"]
    tasks = triage["root"]["dag"]["tasks"]
    assert all(t["cachingOptions"] == {} for t in tasks.values())
    register = tasks["register"]["inputs"]["parameters"]
    assert register["passed"]["taskOutputParameter"]["producerTask"] == "triage-evaluate"
    assert register["version"]["taskOutputParameter"]["producerTask"] == "triage-train"
    assert register["trigger"]["componentInputParameter"] == "trigger"
    assert triage["root"]["dag"]["tasks"]["triage-train"]["dependentTasks"] == ["triage-data-check"]


def test_local_subprocess_runner_runs_triage_end_to_end(tmp_path, small_ticket_file, monkeypatch):
    from kfp import local

    from nw.pipelines.kfp import triage_pipeline
    from nw.pipelines.kfp.run_local import kfp_interpreter

    monkeypatch.setenv("NW_PIPELINE_REGISTRY", "tests.pipelines.fake_registry:build")
    monkeypatch.setenv("NW_FAKE_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("NW_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    local.init(
        runner=local.SubprocessRunner(use_venv=False),
        pipeline_root=str(tmp_path / "pipeline_root"),
        raise_on_error=True,
    )
    with kfp_interpreter():
        run = triage_pipeline(IMAGE)(
            data_uri=str(small_ticket_file),
            output_root=str(tmp_path / "out"),
            tenant="alice",
            environment="northwind",
            production_summary=str(tmp_path / "no_production.json"),
        )
    assert run.outputs["Output"].startswith("northwind-alice-triage version 1 from ")
    out = tmp_path / "out" / "triage"
    gate = json.loads((out / "steps" / "triage_evaluate.json").read_text())
    assert gate["passed"] and gate["metrics"]["p0_recall"] >= 0.85
    registered = json.loads((tmp_path / "registry" / "northwind-alice-triage.json").read_text())
    assert registered[0]["tags"]["artifact_version"] == gate["candidate"]
    assert (out / gate["candidate"] / "MODEL_CARD.md").exists()
    assert not (out / "latest").exists()


def test_local_runner_fails_the_run_when_the_gate_fails(tmp_path, small_ticket_file, monkeypatch):
    from kfp import local

    from nw.pipelines.kfp import triage_pipeline
    from nw.pipelines.kfp.run_local import kfp_interpreter

    monkeypatch.setenv("NW_PIPELINE_REGISTRY", "tests.pipelines.fake_registry:build")
    monkeypatch.setenv("NW_FAKE_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("NW_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    local.init(
        runner=local.SubprocessRunner(use_venv=False),
        pipeline_root=str(tmp_path / "pipeline_root"),
        raise_on_error=True,
    )
    with kfp_interpreter(), pytest.raises(RuntimeError, match="register"):
        triage_pipeline(IMAGE)(
            data_uri=str(small_ticket_file),
            output_root=str(tmp_path / "out"),
            production_summary=str(tmp_path / "no_production.json"),
            min_p0_recall=1.01,
        )
    gate = json.loads((tmp_path / "out" / "triage" / "steps" / "triage_evaluate.json").read_text())
    assert not gate["passed"] and "P0 recall" in gate["reason"]
    assert not (tmp_path / "registry").exists()
