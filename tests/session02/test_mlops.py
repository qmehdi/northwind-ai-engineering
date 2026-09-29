"""The MLOps loop around Project 1: validated data in, a gated and documented model out,
drift watched in the service, and the next version compared before it takes traffic."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nw.triage import service
from nw.triage.backtest import compare
from nw.triage.data_check import Expectations, profile, validate
from nw.triage.model_card import render
from nw.triage.monitor import DriftMonitor, psi
from nw.triage.promote import GatePolicy, gate, promote
from nw.triage.tracking import format_runs, load_runs

pytestmark = pytest.mark.session02


# ----- data validation --------------------------------------------------------------
def test_validate_accepts_the_fixture_and_profiles_it(ticket_rows):
    findings = validate(ticket_rows)
    assert [f for f in findings if f.blocking] == []
    p = profile(ticket_rows, "abc")
    assert p.n == len(ticket_rows) and set(p.splits) == {"train", "val", "test"}
    assert abs(sum(p.priority_share.values()) - 1) < 1e-6
    assert len(p.text_length_bins) == len(p.text_length_hist) + 1


def test_validate_flags_schema_share_and_leaks(ticket_rows):
    rows = [dict(r) for r in ticket_rows]
    rows[0]["priority"] = "P9"
    rows[1]["body"] = ""
    leak = dict(rows[2], split="test")
    rows.append(leak)
    checks = {f.check for f in validate(rows)}
    assert {"schema", "split_leak"} <= checks
    strict = validate(ticket_rows, Expectations(p0_share_max=0.001))
    assert any(f.check == "p0_share" for f in strict)


# ----- drift ------------------------------------------------------------------------
def test_psi_is_zero_for_identical_and_grows_with_shift():
    assert psi([0.25, 0.25, 0.25, 0.25], [0.25, 0.25, 0.25, 0.25]) == pytest.approx(0.0)
    assert psi([0.7, 0.2, 0.1], [0.1, 0.2, 0.7]) > 0.5


def test_monitor_warms_up_then_alerts_on_a_shifted_world(ticket_rows):
    p = profile(ticket_rows, "abc")
    m = DriftMonitor(json.loads(json.dumps(p.__dict__)), window=200, min_window=20)
    assert m.snapshot().level == "warming_up"
    for _ in range(100):
        m.observe(int(p.text_length_quantiles["0.5"]), "P2")
    stable = m.snapshot()
    assert stable.text_length_psi is not None
    for _ in range(200):
        m.observe(50_000, "P0")  # far longer text, every ticket a P0
    assert m.snapshot().level == "alert"


# ----- gate -------------------------------------------------------------------------
def summary(version, macro_f1=0.7, p0_recall=0.9, ece=0.08, brier=0.01, sha="d1"):
    return {
        "version": version,
        "data_sha256_12": sha,
        "test": {
            "macro_f1": macro_f1,
            "p0_recall": p0_recall,
            "p0_precision": 0.4,
            "ece": ece,
            "brier_p0": brier,
        },
        "p0_threshold": 0.05,
    }


def test_gate_first_model_needs_only_the_absolute_bars():
    assert gate(summary("v1"), None).passed
    d = gate(summary("v1", p0_recall=0.6), None)
    assert not d.passed and "P0 recall" in d.reasons[0]


def test_gate_blocks_regressions_and_data_changes():
    prod = summary("v1")
    assert gate(summary("v2", macro_f1=0.69), prod).passed
    worse = gate(summary("v2", macro_f1=0.6), prod)
    assert not worse.passed and any("macro-F1" in r for r in worse.reasons)
    moved = gate(summary("v2", sha="d2"), prod)
    assert not moved.passed and any("test split changed" in r for r in moved.reasons)
    assert gate(summary("v2", p0_recall=0.86), prod, GatePolicy(max_p0_recall_drop=0.05)).passed


def test_promote_points_latest_and_records_the_decision(trained, tmp_path):
    model, report, out = trained
    assert (out / "latest").resolve().name == model.version
    assert (out / "promotions.jsonl").exists()
    assert report["promotion"]["passed"] is True
    d = promote(out, model.version, summary_path=tmp_path / "prod.json")
    assert d.passed and (tmp_path / "prod.json").exists()


# ----- tracking and the card --------------------------------------------------------
def test_run_is_recorded_and_the_card_is_written(trained):
    model, report, out = trained
    runs = load_runs(out / "runs.jsonl")
    assert runs and runs[-1]["version"] == model.version
    assert "macro-F1" in format_runs(runs)
    card = (out / model.version / "MODEL_CARD.md").read_text()
    assert model.version in card and "P0 recall" in card and "drift" in card.lower()
    assert render(model.metadata).startswith("# Model card")


def test_registered_model_is_the_tenants_when_a_tenant_is_set(monkeypatch, tmp_path):
    from nw.triage import tracking

    monkeypatch.delenv("NW_TENANT", raising=False)
    monkeypatch.delenv("NW_ENVIRONMENT", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert tracking.registered_model_name() == "northwind-triage"
    monkeypatch.setenv("NW_TENANT", "alice")
    assert tracking.registered_model_name() == "northwind-alice-triage"
    monkeypatch.setenv("NW_ENVIRONMENT", "northwind-dev")
    assert tracking.registered_model_name() == "northwind-dev-alice-triage"


def test_mlflow_registers_under_the_tenant_name(tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    from nw.triage import tracking

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    monkeypatch.setenv("NW_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.delenv("NW_ENVIRONMENT", raising=False)
    artifact = tmp_path / "v1"
    artifact.mkdir()
    (artifact / "model.joblib").write_bytes(b"m")
    run = {
        "version": "v1",
        "params": {"seed": 0},
        "metrics": {"test_macro_f1": 0.5},
        "data_sha256_12": "abc",
        "git_sha": "def",
    }
    logged = tracking._log_to_mlflow(artifact, run)
    assert logged["registered_model"] == "northwind-alice-triage"
    assert tracking.set_production_alias("v1") is True


def test_by_language_metrics_are_in_the_report(trained):
    _, report, _ = trained
    assert "by_language" in report["test"]


# ----- shadow, capture, drift endpoint ----------------------------------------------
@pytest.fixture
def shadow_client(trained, tmp_path, monkeypatch):
    model, _, out = trained
    monkeypatch.setenv("NW_TRIAGE_MODEL", str(out / "latest"))
    monkeypatch.setenv("NW_TRIAGE_SHADOW_MODEL", str(out / "latest"))
    monkeypatch.setenv("NW_TRIAGE_CAPTURE", str(tmp_path / "predictions.jsonl"))
    monkeypatch.setenv("NW_TRIAGE_DRIFT_MIN", "5")
    monkeypatch.setenv("NW_TRIAGE_DRIFT_EVERY", "5")
    with TestClient(service.app) as c:
        yield c, tmp_path / "predictions.jsonl"


def test_shadow_scores_every_request_and_capture_records_it(shadow_client):
    c, capture = shadow_client
    for i in range(6):
        r = c.post("/triage", json={"subject": "Login broken", "body": f"cannot sign in {i}"})
        assert r.status_code == 200
    assert c.get("/version").json()["shadow_version"]
    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(lines) == 6 and lines[0]["shadow_priority"] == lines[0]["priority"]
    snap = c.get("/drift").json()
    assert snap["window"] == 6 and snap["level"] in {"ok", "watch", "alert"}
    text = c.get("/metrics").text
    assert 'nw_triage_shadow_total{agree="true"}' in text and "nw_triage_drift_psi" in text


def test_backtest_reports_agreement_and_metrics(trained, ticket_rows):
    model, _, _ = trained
    rows = [r for r in ticket_rows if r["split"] == "test"][:100]
    r = compare(model, model, rows)
    assert r["agreement"] == 1.0 and "metrics" in r["a"]


def test_training_refuses_bad_data(tmp_path, ticket_rows):
    from nw.triage.train import train

    bad = tmp_path / "bad.jsonl"
    with bad.open("w") as f:
        for r in ticket_rows[:600]:
            f.write(json.dumps(dict(r, priority="P2")) + "\n")  # no P0 anywhere
    with pytest.raises(SystemExit, match="data check failed"):
        train(bad, tmp_path / "out")


def test_data_check_cli_on_the_real_data(ticket_file):
    from nw.triage.data_check import check_file

    p = check_file(Path(ticket_file))
    assert p.ok, p.findings
