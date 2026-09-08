"""Acceptance 1: the feature pipeline is reproducible and serialisable."""

import joblib
import numpy as np
import pytest

from nw.triage.features import StructuralFeatures, build_features, ticket_text

pytestmark = pytest.mark.session02


def test_same_ticket_same_features(ticket_rows, tmp_path):
    feats = build_features().fit(ticket_rows[:300])
    a = feats.transform(ticket_rows[:5]).toarray()
    b = feats.transform(ticket_rows[:5]).toarray()
    assert np.array_equal(a, b)
    joblib.dump(feats, tmp_path / "f.joblib")
    reloaded = joblib.load(tmp_path / "f.joblib")
    assert np.array_equal(reloaded.transform(ticket_rows[:5]).toarray(), a)


def test_structural_signals_fire_on_logs_and_shouting():
    sf = StructuralFeatures()
    calm = sf.transform(
        [{"subject": "Question", "body": "How do I export a dashboard?"}]
    ).toarray()[0]
    loud = sf.transform(
        [{"subject": "x", "body": "EVERYTHING IS DOWN!!! ERROR 500 ERROR 500 TIMEOUT FAILED"}]
    ).toarray()[0]
    log = sf.transform(
        [
            {
                "subject": "Sync failed",
                "body": "2026-01-01T02:00:00Z ERROR step failed\nTraceback (most recent call last)\n  at sync.run",
            }
        ]
    ).toarray()[0]
    names = list(sf.names)
    assert loud[names.index("caps_ratio")] > 0.8 > calm[names.index("caps_ratio")]
    assert loud[names.index("error_density")] > calm[names.index("error_density")]
    assert log[names.index("log_lines")] > 0
    assert loud[names.index("subject_missing")] == 1.0


def test_subject_is_weighted_twice():
    assert ticket_text("Down", "body").count("Down") == 2
