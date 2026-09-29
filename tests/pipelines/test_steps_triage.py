"""Project 1 steps on the fixture: the same artifact tree and the same metrics as
`nw.triage.train`, the gate through the existing policy, the registry touched only by
the register step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nw.pipelines.steps import read_result, triage_data_check, triage_evaluate, triage_train
from nw.pipelines.steps import register as register_step
from nw.platform.base import Stage
from nw.triage.model import TriageModel
from nw.triage.promote import GatePolicy

pytestmark = pytest.mark.pipelines


@pytest.fixture(scope="module")
def trained_step(tmp_path_factory, small_ticket_file):
    out = tmp_path_factory.mktemp("triage")
    check = triage_data_check.run(small_ticket_file, out)
    train = triage_train.run(small_ticket_file, out, package_dir=out)
    return out, check, train


def test_data_check_writes_profile_and_result(trained_step):
    out, check, _ = trained_step
    assert check["ok"] and check["n"] == 1200 and set(check["splits"]) == {"train", "val", "test"}
    assert (out / "data_profile.json").exists() and (out / "data_check.json").exists()
    assert read_result(out, "triage_data_check")["data_sha256_12"] == check["data_sha256_12"]


def test_data_check_cli_exits_one_on_bad_data(tmp_path, ticket_rows):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(json.dumps(dict(r, priority="P2")) for r in ticket_rows[:600]))
    assert triage_data_check.main(["--data", str(bad), "--out", str(tmp_path / "out")]) == 1
    assert not read_result(tmp_path / "out", "triage_data_check")["ok"]


def test_train_step_is_the_course_training_run(trained_step):
    out, _, train = trained_step
    version = train["version"]
    model = TriageModel.load(out / version)
    assert model.version == version and (out / version / "MODEL_CARD.md").exists()
    assert not (out / "latest").exists(), "the step never promotes"
    report = json.loads((out / version / "report.json").read_text())
    assert train["metrics"]["macro_f1"] == report["test"]["macro_f1"]
    assert train["metrics"]["p0_recall"] >= 0.85, "the fixture clears the absolute bar"
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["version"] == version
    assert Path(train["packaged"]).name == "model.tar.gz"
    import tarfile

    with tarfile.open(train["packaged"]) as tar:
        names = tar.getnames()
    assert "metadata.json" in names and "model.joblib" in names


def test_evaluate_passes_a_first_model_and_records_the_decision(trained_step, tmp_path):
    out, _, train = trained_step
    result = triage_evaluate.run(out, train["version"], production_summary=tmp_path / "none.json")
    assert result["passed"] and result["passed_int"] == 1 and result["production"] is None
    assert result["reason"] == "all bars cleared"
    assert read_result(out, "triage_evaluate")["candidate"] == train["version"]
    gate = json.loads((out / train["version"] / "gate.json").read_text())
    assert gate["metrics"]["p0_recall"] == train["metrics"]["p0_recall"]
    log = [json.loads(line) for line in (out / "promotions.jsonl").read_text().splitlines()]
    assert log[-1]["candidate"] == train["version"] and log[-1]["step"] == "triage_evaluate"


def test_evaluate_blocks_on_a_moved_test_split_and_on_the_bars(trained_step, tmp_path):
    out, _, train = trained_step
    production = tmp_path / "prod.json"
    production.write_text(
        json.dumps(
            {
                "version": "older",
                "data_sha256_12": "000000000000",
                "test": {
                    "macro_f1": 0.5,
                    "p0_recall": 0.9,
                    "p0_precision": 0.4,
                    "ece": 0.05,
                    "brier_p0": 0.01,
                },
                "p0_threshold": 0.3,
            }
        )
    )
    moved = triage_evaluate.run(out, train["version"], production_summary=production)
    assert not moved["passed"] and "test split changed" in moved["reason"]
    strict = triage_evaluate.run(
        out,
        train["version"],
        production_summary=tmp_path / "none.json",
        policy=GatePolicy(min_p0_recall=1.01),
    )
    assert not strict["passed"] and "P0 recall" in strict["reason"]
    forced = triage_evaluate.run(
        out,
        train["version"],
        production_summary=tmp_path / "none.json",
        policy=GatePolicy(min_p0_recall=1.01),
        force=True,
    )
    assert forced["passed"] and forced["forced"] and forced["passed_int"] == 1


def test_evaluate_cli_accepts_a_directory_and_a_force_value(trained_step, tmp_path):
    out, _, train = trained_step
    production_dir = tmp_path / "production"
    production_dir.mkdir()
    (production_dir / "triage_production.json").write_text(
        json.dumps(
            {
                "version": "older",
                "data_sha256_12": train["data_sha256_12"],
                "test": {
                    "macro_f1": 0.5,
                    "p0_recall": 0.9,
                    "p0_precision": 0.4,
                    "ece": 0.05,
                    "brier_p0": 0.01,
                },
                "p0_threshold": 0.3,
            }
        )
    )
    args = ["--out", str(out), "--version", train["version"]]
    args += ["--production-summary", str(production_dir), "--min-p0-recall", "1.01"]
    assert triage_evaluate.main(args + ["--strict"]) == 1
    result = read_result(out, "triage_evaluate")
    assert not result["passed"] and result["production"] == "older"
    assert triage_evaluate.main(args + ["--force", "false", "--strict"]) == 1
    assert triage_evaluate.main(args + ["--force", "--strict"]) == 0
    assert read_result(out, "triage_evaluate")["forced"]


def test_register_uses_the_registry_only_after_a_passed_gate(
    trained_step, tmp_path, registry, tenant
):
    out, _, train = trained_step
    triage_evaluate.run(
        out,
        train["version"],
        production_summary=tmp_path / "none.json",
        policy=GatePolicy(min_p0_recall=1.01),
    )
    with pytest.raises(SystemExit, match="gate failed"):
        register_step.run(out, None, pipeline="triage", registry=registry, tenant=tenant)
    assert registry.versions(tenant, "triage") == []

    triage_evaluate.run(out, train["version"], production_summary=tmp_path / "none.json")
    result = register_step.run(out, None, pipeline="triage", registry=registry, tenant=tenant)
    assert result["version"] == train["version"] and result["tenant"] == "northwind-alice"
    versions = registry.versions(tenant, "triage")
    assert len(versions) == 1 and versions[0].name == "northwind-alice-triage"
    assert versions[0].stage == Stage.CANDIDATE
    assert versions[0].metrics["p0_recall"] == train["metrics"]["p0_recall"]
    assert versions[0].tags["artifact_version"] == train["version"]
    assert versions[0].tags["gate"] == "passed" and versions[0].tags["pipeline"] == "triage"
    assert versions[0].tags["trigger"] == "manual"
    assert json.loads((out / train["version"] / "register.json").read_text())["registered"]
    with pytest.raises(SystemExit, match="rerun the gate"):
        register_step.run(out, "other", pipeline="triage", registry=registry, tenant=tenant)


def test_register_cli_takes_the_registry_from_the_environment(trained_step, tmp_path, monkeypatch):
    out, _, train = trained_step
    triage_evaluate.run(out, train["version"], production_summary=tmp_path / "none.json")
    monkeypatch.setenv("NW_PIPELINE_REGISTRY", "tests.pipelines.fake_registry:build")
    monkeypatch.setenv("NW_FAKE_REGISTRY_DIR", str(tmp_path / "registry"))
    args = ["--pipeline", "triage", "--out", str(out), "--tenant", "bob", "--environment", "dev"]
    assert register_step.main([*args, "--trigger", "schedule"]) == 0
    records = json.loads((tmp_path / "registry" / "dev-bob-triage.json").read_text())
    assert "schedule" in json.dumps(records), "a scheduled run is told apart on the version"
    assert read_result(out, "register")["tenant"] == "dev-bob"
