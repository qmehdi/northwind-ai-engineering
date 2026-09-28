"""The MLOps loop around Project 2: validated data in, a versioned candidate out, the
run recorded, the gate fed by the export and the benchmark, the card generated, drift
and capture in the service, and two versions compared before one takes traffic."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from nw.semantic import service
from nw.semantic.artifacts import newest_candidate, resolve, versions
from nw.semantic.backtest import Predictor, compare
from nw.semantic.model_card import render
from nw.semantic.monitor import SemanticDriftMonitor
from nw.semantic.promote import GatePolicy, gate, promote
from nw.semantic.tracking import format_runs, load_runs

pytestmark = pytest.mark.session03

PERMISSIVE = GatePolicy(
    min_p0_recall=0.0,
    min_tag_micro_f1=0.0,
    max_parity_fp32=1.0,
    max_int8_macro_f1_drop=1.0,
    max_int8_tag_f1_drop=1.0,
    max_int8_p95_ms=1e9,
)


# ----- data contract and the versioned artifact ---------------------------------------
def test_training_refuses_bad_data(tmp_path, s3_rows, spec, config, tiny_tokenizer):
    from nw.semantic.train import train

    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(json.dumps(dict(r, priority="P2")) for r in s3_rows))  # no P0
    with pytest.raises(SystemExit, match="data check failed"):
        train(bad, tmp_path / "out", spec=spec, config=config, tokenizer=tiny_tokenizer, epochs=1)


def test_artifact_is_versioned_and_carries_profile_card_and_test_metrics(trained_tiny):
    out, meta = trained_tiny
    assert re.fullmatch(r"\d{14}-\w+-[0-9a-f]{12}", meta["version"])
    assert out.name == meta["version"] and not (out.parent / "latest").exists()
    profile = json.loads((out / "data_profile.json").read_text())
    assert profile["data_sha256_12"] == meta["data_sha256_12"]
    assert sum(profile["tag_share"].values()) > 0 and "text_length_bins" in profile
    for split in ("val", "test"):
        m = meta["metrics"][split]
        assert {
            "tag_micro_f1",
            "tag_macro_f1",
            "priority_macro_f1",
            "p0_recall",
            "by_language",
        } <= set(m)
    assert meta["metrics"]["test"]["by_language"]["en"]["n"] == meta["n_test"]
    assert len(meta["history"]) == meta["epochs"]
    card = (out / "MODEL_CARD.md").read_text()
    assert meta["version"] in card and "Performance by language" in card and "drift" in card.lower()


def test_run_is_recorded_with_params_history_and_test_metrics(trained_tiny):
    out, meta = trained_tiny
    runs = load_runs(out.parent / "runs.jsonl")
    assert runs and runs[-1]["version"] == meta["version"]
    assert set(runs[-1]["params"]) == {
        "subset",
        "epochs",
        "lr",
        "r",
        "alpha",
        "dropout",
        "max_length",
        "seed",
    }
    assert len(runs[-1]["history"]) == meta["epochs"] and runs[-1]["metrics"]["seconds"] >= 0
    assert "tag micro-F1" in format_runs(runs) and meta["version"] in format_runs(runs)


def test_resolve_root_version_and_flat_directory(trained_tiny, tmp_path):
    out, meta = trained_tiny
    root = out.parent
    assert resolve(out) == out and resolve(root) == newest_candidate(root)
    assert versions(root) and versions(root)[-1] == meta["version"]
    with pytest.raises(FileNotFoundError, match="promotion gate"):
        resolve(root, serve=True)  # no latest until the gate sets it
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "metadata.json").write_text("{}")
    assert resolve(flat) == flat and resolve(flat, serve=True) == flat  # the Colab zip
    with pytest.raises(FileNotFoundError, match="train-semantic"):
        resolve(tmp_path / "empty")


# ----- the gate -------------------------------------------------------------------------
def _summary(version, sha="d1", **over):
    s = {
        "version": version,
        "data_sha256_12": sha,
        "test": {
            "tag_micro_f1": 0.54,
            "tag_macro_f1": 0.6,
            "priority_macro_f1": 0.69,
            "p0_recall": 0.74,
        },
        "export": {"max_abs_diff_fp32": 1e-5, "max_abs_diff_int8": 1.1, "size_ratio": 3.98},
        "benchmark": {
            "fp32": {"priority_macro_f1": 0.69, "p0_recall": 0.74, "tag_micro_f1": 0.54},
            "int8": {
                "priority_macro_f1": 0.67,
                "p0_recall": 0.65,
                "tag_micro_f1": 0.52,
                "p50_ms": 9.0,
                "p95_ms": 13.0,
            },
        },
    }
    for k, v in over.items():
        section, key = k.split("__")
        target = s[section]
        if section == "benchmark":
            target = s["benchmark"]["int8"]
        target[key] = v
    return s


def test_gate_first_model_needs_only_the_absolute_bars():
    assert gate(_summary("v1"), None).passed
    d = gate(_summary("v1", test__p0_recall=0.6, test__tag_micro_f1=0.4), None)
    assert not d.passed and len(d.reasons) == 2 and "P0 recall" in d.reasons[0]
    assert not gate(_summary("v1", export__max_abs_diff_fp32=1e-3), None).passed
    slow = gate(_summary("v1", benchmark__p95_ms=250.0), None)
    assert not slow.passed and "p95" in slow.reasons[0]
    lossy = gate(_summary("v1", benchmark__priority_macro_f1=0.60), None)
    assert not lossy.passed and "int8 priority macro-F1" in lossy.reasons[0]


def test_gate_blocks_regressions_and_data_changes():
    prod = _summary("v1")
    assert gate(_summary("v2", test__tag_micro_f1=0.53), prod).passed
    worse = gate(_summary("v2", test__tag_micro_f1=0.50), prod)
    assert not worse.passed and any("tag micro-F1" in r for r in worse.reasons)
    moved = gate(_summary("v2", sha="d2"), prod)
    assert not moved.passed and any("test split changed" in r for r in moved.reasons)
    assert gate(
        _summary("v2", test__priority_macro_f1=0.64),
        prod,
        GatePolicy(max_priority_macro_f1_drop=0.1),
    ).passed


def test_gate_names_the_missing_export_and_benchmark(trained_tiny, tmp_path):
    out, meta = trained_tiny
    with pytest.raises(SystemExit, match="export_report.json is missing"):
        promote(out.parent, meta["version"], summary_path=tmp_path / "prod.json")


def test_export_then_benchmark_then_gate_moves_latest(benchmarked_tiny, tmp_path):
    out, results = benchmarked_tiny
    assert (out / "export_report.json").exists() and (out / "benchmark.json").exists()
    assert [r["model"] for r in results][1:] == [
        "Project 2: PyTorch fp32",
        "Project 2: ONNX fp32",
        "Project 2: ONNX int8",
    ]
    root, version = out.parent, out.name
    d = promote(root, version, policy=PERMISSIVE, summary_path=tmp_path / "prod.json")
    assert d.passed and not d.forced and d.production is None
    assert (root / "latest").resolve() == out.resolve()
    assert resolve(root, serve=True) == root / "latest"
    log = [json.loads(line) for line in (root / "promotions.jsonl").read_text().splitlines()]
    assert log[-1]["candidate"] == version and log[-1]["passed"] is True
    summary = json.loads((tmp_path / "prod.json").read_text())
    assert summary["version"] == version and "benchmark" in summary and "promoted_at" in summary
    card = (out / "MODEL_CARD.md").read_text()
    assert "Benchmark against Project 1" in card and "Export and serving" in card
    # Promoting the served model again compares against nothing and passes.
    again = promote(root, version, policy=PERMISSIVE, summary_path=tmp_path / "prod.json")
    assert again.passed and again.production is None


def test_forced_promotion_is_recorded(benchmarked_tiny, tmp_path):
    out, _ = benchmarked_tiny
    strict = GatePolicy(min_tag_micro_f1=1.01)  # nothing clears this
    d = promote(out.parent, out.name, policy=strict, force=True, summary_path=tmp_path / "p.json")
    assert d.passed and d.forced and d.reasons


# ----- drift --------------------------------------------------------------------------
def test_tag_rate_drift_alerts_on_a_new_tag_mix(trained_tiny):
    out, _ = trained_tiny
    profile = json.loads((out / "data_profile.json").read_text())
    m = SemanticDriftMonitor(profile, window=200, min_window=20)
    assert m.snapshot().level == "warming_up"
    median = int(profile["text_length_quantiles"]["0.5"])
    common = sorted(profile["tag_share"], key=profile["tag_share"].get, reverse=True)[:2]
    for _ in range(60):
        m.observe(median, "P2", common)
    steady = m.snapshot()
    assert steady.tag_rate_psi is not None and steady.text_length_psi is not None
    for _ in range(200):
        m.observe(median, "P2", ["GDPR", "Backup"])  # tags the training rows never carried
    shifted = m.snapshot()
    assert shifted.tag_rate_psi > steady.tag_rate_psi and shifted.level == "alert"
    assert set(shifted.tag_share) == {"GDPR", "Backup"}


# ----- the service on latest: drift, capture, version -----------------------------------
@pytest.fixture
def served(benchmarked_tiny, tmp_path, monkeypatch):
    out, _ = benchmarked_tiny
    promote(out.parent, out.name, policy=PERMISSIVE, summary_path=tmp_path / "prod.json")
    monkeypatch.setenv("NW_SEMANTIC_ARTIFACT", str(out.parent))  # the root means latest
    monkeypatch.delenv("NW_INDEX", raising=False)
    monkeypatch.setenv("NW_SEMANTIC_CAPTURE", str(tmp_path / "predictions.jsonl"))
    monkeypatch.setenv("NW_SEMANTIC_DRIFT_MIN", "5")
    monkeypatch.setenv("NW_SEMANTIC_DRIFT_EVERY", "5")
    with TestClient(service.app) as c:
        yield c, out, tmp_path / "predictions.jsonl"


def test_service_serves_latest_reports_drift_and_captures(served):
    c, out, capture = served
    assert c.get("/readyz").json()["model_version"] == out.name
    assert c.get("/version").json()["metadata"]["data_sha256_12"]
    assert c.get("/drift").json()["level"] == "warming_up"
    for i in range(6):
        r = c.post(
            "/classify",
            json={"subject": "production down", "body": f"outage all users {i}", "k": 0},
        )
        assert r.status_code == 200
    snap = c.get("/drift").json()
    assert snap["window"] == 6 and snap["level"] in {"ok", "watch", "alert"}
    assert snap["tag_rate_psi"] is not None and "tag_share" in snap
    text = c.get("/metrics").text
    assert 'nw_semantic_drift_psi{feature="tag_rate"}' in text and "nw_semantic_drift_level" in text
    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(lines) == 6 and {"tags", "priority", "model_version", "format"} <= set(lines[0])


@pytest.fixture
def shadowed(benchmarked_tiny, tmp_path, monkeypatch):
    """The served artifact as its own shadow: the two int8 graphs cannot disagree."""
    out, _ = benchmarked_tiny
    promote(out.parent, out.name, policy=PERMISSIVE, summary_path=tmp_path / "prod.json")
    monkeypatch.setenv("NW_SEMANTIC_ARTIFACT", str(out.parent / "latest"))
    monkeypatch.setenv("NW_SEMANTIC_SHADOW_ARTIFACT", str(out))  # the version directory
    monkeypatch.delenv("NW_INDEX", raising=False)
    monkeypatch.setenv("NW_SEMANTIC_CAPTURE", str(tmp_path / "predictions.jsonl"))
    with TestClient(service.app) as c:
        yield c, out, tmp_path / "predictions.jsonl"


def test_shadow_scores_every_request_and_capture_records_it(shadowed):
    c, out, capture = shadowed
    assert c.get("/version").json()["shadow_version"] == out.name
    for i in range(6):
        r = c.post("/classify", json={"subject": "Login broken", "body": f"cannot sign in {i}"})
        assert r.status_code == 200 and "shadow" not in r.json()  # never served
    text = c.get("/metrics").text
    assert 'nw_semantic_shadow_total{agree="true"} 6.0' in text
    assert 'nw_semantic_shadow_total{agree="false"}' not in text
    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(lines) == 6 and all(x["shadow_priority"] == x["priority"] for x in lines)


# ----- backtest and the card ------------------------------------------------------------
def test_backtest_reports_agreement_and_metrics(benchmarked_tiny, s3_rows):
    out, _ = benchmarked_tiny
    rows = [r for r in s3_rows if r["split"] == "test"]
    a = Predictor(out)
    r = compare(a, Predictor(out, quantized=False), rows)
    assert a.format == "model.int8.onnx" and r["n"] == len(rows)
    assert 0.0 <= r["priority_agreement"] <= 1.0 and 0.0 <= r["tag_jaccard"] <= 1.0
    assert {"tag_micro_f1", "priority_macro_f1", "p0_recall"} <= set(r["a"]["metrics"])
    same = compare(a, a, rows)
    assert (
        same["priority_agreement"] == 1.0
        and same["tag_set_agreement"] == 1.0
        and not same["disagreements"]
    )


def test_model_card_renders_from_metadata_alone(trained_tiny):
    _, meta = trained_tiny
    card = render(meta)
    assert (
        card.startswith("# Model card") and "| P0 recall |" in card and "Tag thresholds" not in card
    )
