"""`nw.pipelines.retrain` submits through the platform's PipelineRunner with the pipeline's
parameter names and nothing else, and the parameter schema is what both SDKs derive from."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from nw.pipelines import params, retrain
from nw.platform.base import PipelineRun, RunStatus, Tenant

pytestmark = pytest.mark.pipelines


@dataclass
class FakeRunner:
    submitted: list[tuple] = field(default_factory=list)

    def submit(self, tenant, pipeline, parameters):
        self.submitted.append((tenant, pipeline, dict(parameters)))
        return PipelineRun(pipeline=pipeline, run_id="run-1", status=RunStatus.QUEUED, url="u")

    def status(self, tenant, run):
        return run

    def wait(self, tenant, run, timeout_s=1800):
        return PipelineRun(
            pipeline=run.pipeline,
            run_id=run.run_id,
            status=RunStatus.SUCCEEDED,
            outputs={"Output": "registered"},
        )

    def logs(self, tenant, run):
        yield "done"


@dataclass
class FakePlatform:
    pipelines: FakeRunner


def test_main_submits_only_what_was_set(monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.setenv("NW_TRACK", "local")
    monkeypatch.setattr("nw.platform.platform_for", lambda cfg: FakePlatform(runner))
    monkeypatch.setattr("nw.config.settings", lambda: type("S", (), {"tenant": "alice"})())
    monkeypatch.setattr(
        "nw.platform.base.tenant_from_env",
        lambda cfg: Tenant(name="alice", environment="northwind"),
    )
    code = retrain.main(
        ["--pipeline", "triage", "--min-p0-recall", "0.9", "--force", "true", "--wait"]
    )
    assert code == 0
    [(tenant, pipeline, values)] = runner.submitted
    assert tenant.prefix == "northwind-alice" and pipeline == "triage"
    assert values == {
        "min_p0_recall": 0.9,
        "force": True,
        "tenant": "alice",
        "environment": "northwind",
    }
    out = capsys.readouterr().out
    assert "submitted triage for northwind-alice: run run-1 queued" in out
    assert "run run-1 succeeded" in out and "Output: registered" in out


def test_an_in_process_runner_is_followed_without_wait(capsys):
    """The Local track's runner lives in the submitting process, so `make pipeline-submit`
    waits for it instead of killing the run on exit; a cloud runner returns at once."""
    tenant = Tenant(name="solo", environment="northwind")
    cloud = retrain.submit(FakePlatform(FakeRunner()), tenant, "triage", {})
    assert cloud.status == RunStatus.QUEUED
    local_runner = FakeRunner()
    local_runner.runs_in_process = True  # type: ignore[attr-defined]
    local = retrain.submit(FakePlatform(local_runner), tenant, "triage", {})
    assert local.status == RunStatus.SUCCEEDED
    assert "following the run to its end" in capsys.readouterr().out


def test_semantic_flags_are_the_workflow_inputs(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr("nw.platform.platform_for", lambda cfg: FakePlatform(runner))
    monkeypatch.setattr("nw.config.settings", lambda: object())
    monkeypatch.setattr(
        "nw.platform.base.tenant_from_env", lambda cfg: Tenant(name="solo", environment="northwind")
    )
    assert retrain.main(["--pipeline", "semantic", "--epochs", "3", "--subset", "500"]) == 0
    values = runner.submitted[0][2]
    assert values["epochs"] == 3 and values["subset"] == 500 and values["tenant"] == "solo"


def test_schema_names_and_sagemaker_names():
    names = {p.name for p in params.TRIAGE}
    assert {
        "data_uri",
        "output_root",
        "min_p0_recall",
        "max_ece",
        "force",
        "register_model",
    } <= names
    assert {p.name for p in params.SEMANTIC} >= {"epochs", "subset", "lr", "min_tag_micro_f1"}
    assert params.sagemaker_parameter_name("min_p0_recall") == "MinP0Recall"
    assert params.defaults("triage")["min_p0_recall"] == 0.85
    assert not next(p for p in params.TRIAGE if p.name == "tenant").sagemaker
