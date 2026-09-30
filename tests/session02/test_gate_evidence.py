"""The gate states its evidence: intervals on small samples, missed P0 tickets counted and
paired on the same rows, per-language bars with a minimum slice size, written waivers, who
promoted and why, and a threshold that records whether it met its target."""

import json

import numpy as np
import pytest

from nw.evalstats import (
    as_count,
    bootstrap_ci,
    chi2_ppf,
    mcnemar,
    paired_bootstrap,
    pass_at_k,
    pass_hat_k,
    psi_critical,
    wilson,
)
from nw.triage.promote import Decision, GatePolicy, format_decision, gate, promote
from nw.triage.train import THRESHOLD_GRID, calibration_split, choose_p0_threshold, threshold_report

pytestmark = pytest.mark.session02


# ----- the statistics ----------------------------------------------------------------
def test_wilson_interval_matches_the_audit_numbers():
    lo, hi = wilson(28, 31)
    assert 0.74 < lo < 0.76 and 0.96 < hi < 0.97
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(0, 14)
    assert lo == pytest.approx(0.0, abs=1e-9) and 0.2 < hi < 0.23  # 0/14 is compatible with 21%


def test_paired_tests_and_bootstrap():
    assert mcnemar(0, 0) == 1.0
    assert mcnemar(0, 6) < 0.05 < mcnemar(1, 3)
    diff = paired_bootstrap([1.0] * 50, [0.0] * 5 + [1.0] * 45)
    assert diff["diff"] == pytest.approx(-0.1) and diff["hi"] < 0
    lo, hi = bootstrap_ci([0.0, 1.0] * 20)
    assert lo < 0.5 < hi


def test_pass_hat_k_is_stricter_than_pass_at_k():
    runs = [[True, True, True], [True, False, True], [False, False, False]]
    assert pass_hat_k(runs) == pytest.approx(1 / 3) and pass_at_k(runs) == pytest.approx(2 / 3)


def test_psi_chance_level_shrinks_with_the_window():
    assert chi2_ppf(0.99, 9) == pytest.approx(21.67, rel=0.01)
    assert psi_critical(50, 10) > 0.4 > psi_critical(200, 10) > 0.1
    assert as_count(0.7419354838709677) == (23, 31) and as_count(0.5, 40) == (20, 40)


# ----- the threshold ---------------------------------------------------------------------
def test_threshold_grid_reaches_below_the_old_floor_and_picks_the_highest_passing():
    assert min(THRESHOLD_GRID) == 0.01 and max(THRESHOLD_GRID) == 0.95
    y = np.asarray(["P0"] * 10 + ["P2"] * 90)
    proba = np.concatenate([np.full(10, 0.6), np.linspace(0.0, 0.3, 90)])
    t = choose_p0_threshold(proba, y, target_recall=0.9, min_precision=0.2)
    assert t == pytest.approx(0.6), "the highest threshold at the target has fewest false alarms"
    rep = threshold_report(proba, y, t, 0.9, 0.2)
    assert rep["target_met"] and rep["recall"]["k"] == 10 and rep["precision"]["n"] == 10
    unreachable = threshold_report(proba, y, 0.9, 0.9, 0.2)
    assert unreachable["target_met"] is False


def test_calibration_and_threshold_halves_are_disjoint_stratified_and_stable():
    rows = [{"ticket_id": f"T-{i:06d}", "priority": f"P{i % 4}"} for i in range(40)]
    cal, thr = calibration_split(rows)
    assert len(cal) == len(thr) == 20
    assert not {r["ticket_id"] for r in cal} & {r["ticket_id"] for r in thr}
    assert sum(1 for r in cal if r["priority"] == "P0") == 5
    assert calibration_split(list(reversed(rows))) == (cal, thr)


# ----- the gate --------------------------------------------------------------------------
def summ(version, *, tp=28, n=31, macro_f1=0.68, pred=None, truth=None, gate_set=None, met=True):
    s = {
        "version": version,
        "data_sha256_12": "d1",
        "test": {
            "macro_f1": macro_f1,
            "p0_recall": tp / n,
            "p0_precision": 0.5,
            "ece": 0.08,
            "brier_p0": 0.014,
        },
        "test_counts": {"p0_tp": tp, "n_p0": n},
        "p0_threshold": 0.07,
        "threshold": {"target_met": met, "target_recall": 0.9},
    }
    if pred is not None:
        s["test_predictions"] = {"ids_sha256_12": "ids", "pred": pred, "truth": truth}
    if gate_set is not None:
        s["gate_set"] = {"by_language": gate_set}
    return s


def test_one_missed_ticket_is_within_the_bar_two_are_not():
    prod = summ("v1")
    assert gate(summ("v2", tp=27), prod).passed
    d = gate(summ("v2", tp=26), prod)
    assert not d.passed and any("misses 2 more P0 tickets" in r for r in d.reasons)
    assert d.evidence["p0_recall"]["n"] == 31


def test_paired_comparison_uses_the_same_rows():
    truth = "0" * 10 + "2" * 30
    prod_pred = "0" * 9 + "2" + "2" * 30
    cand_pred = "0" * 7 + "22" + "0" + "2" * 30  # misses two production caught, catches one
    prod = summ("v1", tp=9, n=10, pred=prod_pred, truth=truth)
    cand = summ("v2", tp=8, n=10, pred=cand_pred, truth=truth)
    d = gate(cand, prod, GatePolicy(min_p0_recall=0.7))
    paired = d.evidence["paired"]["p0_hits"]
    assert paired == {
        "n": 10,
        "champion_only": 2,
        "challenger_only": 1,
        "net_change": -1,
        "mcnemar_p": pytest.approx(1.0),
    }
    assert d.passed and any("McNemar" in n for n in d.notes)


def test_per_language_bars_need_evidence_and_waivers_are_written():
    few = {"de": {"n": 39, "n_p0": 2, "p0_tp": 1}, "en": {"n": 755, "n_p0": 53, "p0_tp": 45}}
    d = gate(summ("v1", gate_set=few), None)
    assert d.passed and d.insufficient_evidence and "de: 2 P0" in d.insufficient_evidence[0]
    low = {"de": {"n": 97, "n_p0": 24, "p0_tp": 4}, "en": {"n": 779, "n_p0": 53, "p0_tp": 40}}
    d = gate(summ("v1", gate_set=low), None)
    assert d.passed and "de" in d.waived and any(n.startswith("WAIVED de") for n in d.notes)
    strict = GatePolicy(slice_waivers={})
    d = gate(summ("v1", gate_set=low), None, strict)
    assert not d.passed and any("de P0 recall on the gate set" in r for r in d.reasons)
    en_low = {"en": {"n": 779, "n_p0": 53, "p0_tp": 30}}
    assert not gate(summ("v1", gate_set=en_low), None).passed


def test_a_missed_threshold_target_is_named():
    d = gate(summ("v1", met=False), None)
    assert any("missed its P0 recall target" in n for n in d.notes)


def test_forced_promotion_needs_a_reason_and_records_who(trained, tmp_path, monkeypatch):
    model, _, out = trained
    monkeypatch.setenv("NW_ACTOR", "alice")
    with pytest.raises(SystemExit, match="--reason"):
        promote(out, model.version, force=True, summary_path=tmp_path / "p.json")
    d = promote(out, model.version, summary_path=tmp_path / "p.json", reason="first model")
    assert d.decided_by == "alice" and d.reason == "first model"
    written = json.loads((out / model.version / "promotion.json").read_text())
    assert written["decided_by"] == "alice"
    assert "decided by alice" in format_decision(Decision(**written))


def test_training_records_the_threshold_target_and_the_paired_predictions(trained):
    model, report, _ = trained
    assert report["threshold"]["target_met"] in (True, False)
    assert report["threshold"]["rows"] > 0
    preds = report["test"]["predictions"]
    assert len(preds["pred"]) == report["test"]["n"] == len(preds["truth"])
    assert report["test"]["p0_recall_ci"]["n"] == report["test"]["n_p0"]
    assert (
        model.metadata["n_calibration"] + model.metadata["n_threshold"] == model.metadata["n_val"]
    )
