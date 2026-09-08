"""Acceptance 2 to 4: the model beats the baseline, P0 recall is protected, the
artifact is versioned and self-describing."""

import json

import numpy as np
import pytest

from nw.triage.features import PRIORITIES
from nw.triage.model import TriageModel
from nw.triage.train import build_pipeline, by_split, choose_p0_threshold, evaluate, labels

pytestmark = pytest.mark.session02


def test_macro_f1_beats_majority_baseline(trained):
    _, report, _ = trained
    assert report["test"]["macro_f1"] > report["baseline_majority"]["macro_f1"] + 0.3


def test_p0_recall_is_protected_by_the_threshold(trained):
    _, report, _ = trained
    assert report["test"]["p0_recall"] >= 0.8
    assert 0.05 <= report["p0_threshold"] <= 0.95


def test_class_weights_matter_for_p0(ticket_rows):
    """Without class weights and with plain argmax, the rare class is under-predicted."""
    splits = by_split(ticket_rows)
    weighted = build_pipeline(class_weight="balanced").fit(splits["train"], labels(splits["train"]))
    plain = build_pipeline(class_weight=None).fit(splits["train"], labels(splits["train"]))
    test = splits["test"]
    y = labels(test)
    p0_w = (weighted.predict(test) == "P0")[y == "P0"].mean()
    p0_p = (plain.predict(test) == "P0")[y == "P0"].mean()
    assert p0_w >= p0_p


def test_threshold_sweep_prefers_recall_target():
    rng = np.random.default_rng(0)
    y = np.asarray(["P0"] * 40 + ["P2"] * 960)
    proba = np.concatenate([rng.uniform(0.3, 0.9, 40), rng.uniform(0.0, 0.32, 960)])
    t = choose_p0_threshold(proba, y, target_recall=0.9, min_precision=0.2)
    pred = proba >= t
    assert pred[:40].mean() >= 0.9


def test_artifact_is_versioned_and_reloadable(trained):
    model, report, out = trained
    path = out / model.version
    meta = json.loads((path / "metadata.json").read_text())
    for key in (
        "version",
        "trained_at",
        "data_sha256_12",
        "git_sha",
        "classes",
        "p0_threshold",
        "model_sha256",
        "metrics",
    ):
        assert key in meta, key
    reloaded = TriageModel.load(path)
    sample = [
        {
            "subject": "URGENT production down",
            "body": "Everything is down for all users, critical outage.",
        }
    ]
    assert reloaded.predict(sample)[0].model_dump() == model.predict(sample)[0].model_dump()
    assert (out / "latest").resolve() == path.resolve()


def test_corrupt_artifact_is_refused(trained):
    model, _, out = trained
    path = out / model.version
    (path / "model.joblib").write_bytes(b"not a model")
    with pytest.raises(ValueError, match="checksum"):
        TriageModel.load(path)


def test_evaluate_reports_calibration(trained):
    model, _, _ = trained
    rows = [
        {
            "subject": "Question about plan limits",
            "body": "What is the widget limit? Just curious.",
            "priority": "P3",
        }
    ] * 20
    rep = evaluate(model, rows)
    assert set(rep) >= {"macro_f1", "p0_recall", "confusion", "brier_p0", "ece"}
    assert len(rep["confusion"]) == len(PRIORITIES)
