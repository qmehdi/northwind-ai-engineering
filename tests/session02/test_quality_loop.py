"""Monitoring that means something: sample-size aware drift, predictions compared with the
validation predictions, quality gauges for the canary, a central path over captures, live
P0 recall from delayed labels, and a retraining trigger that skips when nothing changed."""

import datetime as dt
import json
import random

import pytest
from prometheus_client import REGISTRY

from nw.triage import monitor as mon
from nw.triage.monitor import DriftMonitor, central_snapshot, live_quality, retrain_trigger

pytestmark = pytest.mark.session02

PROFILE = {
    "text_length_bins": [0.0, 401.0, 444.0, 501.0, 575.0, 643.0, "inf"],
    "text_length_hist": [0.1, 0.146, 0.252, 0.252, 0.15, 0.1],
    "priority_share": {"P0": 0.04, "P1": 0.32, "P2": 0.42, "P3": 0.22},
    "predicted_share": {"P0": 0.065, "P1": 0.31, "P2": 0.42, "P3": 0.205},
}
MIDS = [300, 420, 470, 540, 610, 700]


def _draw(rng, share, keys):
    x, acc = rng.random(), 0.0
    for k in keys:
        acc += share[k] if isinstance(share, dict) else share[keys.index(k)]
        if x < acc:
            return k
    return keys[-1]


def test_a_stationary_stream_rarely_alarms_at_the_default_window():
    rng = random.Random(7)
    alerts = 0
    for _ in range(200):
        m = DriftMonitor(PROFILE)
        for _ in range(200):
            m.observe(
                _draw(rng, PROFILE["text_length_hist"], MIDS),
                _draw(rng, PROFILE["predicted_share"], ["P0", "P1", "P2", "P3"]),
            )
        alerts += m.snapshot().level == "alert"
    assert m.min_window == 200 and alerts <= 4, f"{alerts} of 200 stationary windows alarmed"


def test_small_windows_raise_the_bar_instead_of_alarming_on_noise():
    watch, alert = mon.bars(50, 6)
    assert alert > 0.3 and mon.bars(5000, 6) == (mon.WATCH, mon.ALERT)


def test_prediction_drift_compares_with_validation_predictions_not_labels():
    m = DriftMonitor(PROFILE, min_window=100)
    for i in range(200):
        m.observe(MIDS[i % 6], ["P0", "P1", "P1", "P2", "P2", "P3"][i % 6] if i % 15 else "P0")
    snap = m.snapshot()
    assert snap.baseline == "validation predictions"
    legacy = DriftMonitor({k: v for k, v in PROFILE.items() if k != "predicted_share"})
    assert mon.expected_prediction_share(legacy.profile)[1].startswith("training labels")


def test_p0_share_and_shadow_agreement_reach_the_quality_gauges():
    m = DriftMonitor(PROFILE, min_window=100)
    for i in range(300):
        m.observe(MIDS[i % 6], "P0" if i % 4 == 0 else "P2", shadow_priority="P2")
    snap = m.snapshot()
    assert snap.quality_level == "alert" and snap.p0_share_ratio > 3
    assert snap.shadow_agreement == pytest.approx(0.75)
    assert REGISTRY.get_sample_value("nw_triage_quality_level") == 2
    assert REGISTRY.get_sample_value("nw_triage_p0_share_ratio") == pytest.approx(
        snap.p0_share_ratio
    )
    assert REGISTRY.get_sample_value("nw_triage_shadow_agreement") == pytest.approx(0.75)
    ok = DriftMonitor(PROFILE, min_window=100)
    rng = random.Random(1)
    for _ in range(400):
        ok.observe(470, _draw(rng, PROFILE["predicted_share"], ["P0", "P1", "P2", "P3"]))
    assert ok.snapshot().quality_level == "ok"


def test_central_snapshot_reads_every_instances_capture(tmp_path):
    rows = [
        {"subject": "s", "body": "x" * 460, "priority": "P2", "shadow_priority": "P2"}
        for _ in range(250)
    ]
    for i, part in enumerate((rows[:120], rows[120:])):
        (tmp_path / f"instance-{i}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in part))
    snap = central_snapshot(PROFILE, mon.read_jsonl([str(tmp_path / "instance-*.jsonl")]))
    assert snap.window == 250 and snap.shadow_agreement == 1.0 and snap.level != "warming_up"


def test_live_quality_waits_for_the_label_lag():
    now = dt.datetime(2026, 9, 30, tzinfo=dt.UTC)
    old = (now - dt.timedelta(days=5)).isoformat()
    fresh = (now - dt.timedelta(hours=5)).isoformat()
    captures = [{"ticket_id": f"T-{i}", "priority": "P0" if i < 8 else "P2"} for i in range(40)]
    outcomes = [
        {"ticket_id": f"T-{i}", "final_priority": "P0", "labelled_at": old} for i in range(10)
    ]
    outcomes += [{"ticket_id": "T-30", "final_priority": "P0", "labelled_at": fresh}]
    q = live_quality(captures, outcomes, label_lag_days=3, now=now)
    assert q["labelled"] == 10 and q["p0_recall"]["k"] == 8 and q["p0_recall"]["n"] == 10
    assert q["p0_precision"]["rate"] == 1.0


def test_retrain_trigger_skips_the_same_data_and_fires_on_a_signal():
    prod = {"data_sha256_12": "abc"}
    same = retrain_trigger(data_sha="abc", production=prod)
    assert same["retrain"] is False and "skip" in same["reasons"][0]
    assert retrain_trigger(data_sha="def", production=prod)["retrain"]
    assert retrain_trigger(data_sha="abc", production=prod, new_labels=600)["retrain"]
    drift = {"level": "alert", "window": 900, "quality_level": "ok"}
    assert retrain_trigger(data_sha="abc", production=prod, drift=drift)["retrain"]
    low = {"p0_recall": {"k": 15, "n": 25, "rate": 0.6}}
    assert retrain_trigger(data_sha="abc", production=prod, quality=low)["retrain"]
    few = {"p0_recall": {"k": 3, "n": 5, "rate": 0.6}}
    assert not retrain_trigger(data_sha="abc", production=prod, quality=few)["retrain"]


def test_trigger_cli_writes_the_github_output(tmp_path):
    data = tmp_path / "t.jsonl"
    data.write_text("{}\n")
    summary = tmp_path / "s.json"
    summary.write_text(json.dumps({"data_sha256_12": mon._sha12(data)}))
    out = tmp_path / "gh.txt"
    args = ["trigger", "--data", str(data), "--summary", str(summary), "--github-output", str(out)]
    assert mon.main(args) == 0 and out.read_text() == "retrain=false\n"


def test_quality_alert_line_is_rate_limited_and_names_the_signal(caplog):
    from nw.quality import QualityAlerter

    now = [0.0]
    alerter = QualityAlerter("triage", interval_s=60, clock=lambda: now[0])
    with caplog.at_level("WARNING", logger="nw.quality"):
        assert alerter.check("p0_share", True, value=0.25, bar=2.0, window=300)
        assert not alerter.check("p0_share", True, value=0.25, bar=2.0, window=300)
        assert not alerter.check("shadow_agreement", False, value=0.99, bar=0.9, window=300)
        now[0] = 61
        assert alerter.check("p0_share", True, value=0.25, bar=2.0, window=300)
    lines = [r for r in caplog.records if r.getMessage() == "quality_alert"]
    assert len(lines) == 2
    fields = lines[0].nw
    assert fields["signal"] == "p0_share" and fields["service"] == "triage" and "tenant" in fields


def test_quality_gauges_reach_the_exported_metrics_line():
    from nw import metrics_export as mx

    m = DriftMonitor(PROFILE, min_window=100)
    for i in range(300):
        m.observe(MIDS[i % 6], "P0" if i % 4 == 0 else "P2", shadow_priority="P2")
    m.snapshot()
    fields = mx.Exporter("triage").snapshot()
    assert fields["quality_level"] == 2 and fields["shadow_agreement"] == pytest.approx(0.75)
    assert fields["p0_share_ratio"] > 3 and 0 < fields["p0_share"] < 1
    assert mx.emf_name("p0_share_ratio") == "P0ShareRatio"
    assert set(mx.SERIES["agent"].quality) == {"quality_level", "judge_score"}
