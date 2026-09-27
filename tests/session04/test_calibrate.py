"""The judge is an instrument; the calibration report has to catch the failure that matters,
an unfaithful answer waved through."""

import pytest

from nw.policy.calibrate import CASES, agreement, calibrate, load_cases

pytestmark = pytest.mark.session04


def test_agreement_kappa_and_error_rates():
    pairs = [(True, True)] * 6 + [(False, False)] * 2 + [(False, True)] * 1 + [(True, False)] * 1
    r = agreement(pairs)
    assert r["n"] == 10 and r["agreement"] == 0.8
    assert r["confusion"] == {"tp": 6, "tn": 2, "fp": 1, "fn": 1}
    assert r["false_pass_rate"] == pytest.approx(1 / 3)
    assert r["false_fail_rate"] == pytest.approx(1 / 7)
    assert 0.4 < r["kappa"] < 0.6  # observed 0.8, expected 0.58 for this split


def test_perfect_and_empty():
    assert agreement([(True, True), (False, False)])["kappa"] == 1.0
    with pytest.raises(ValueError):
        agreement([])


def test_shipped_cases_are_balanced_and_self_contained():
    cases = load_cases(CASES)
    assert len(cases) >= 20
    unfaithful = [c for c in cases if not c.human_faithful]
    assert len(unfaithful) >= 8, (
        "the set must contain enough unfaithful answers to measure false passes"
    )
    assert all(len(c.passage) > 80 and c.note for c in cases)
    assert len({c.id for c in cases}) == len(cases)


async def test_calibrate_reports_disagreements_with_threshold():
    cases = load_cases(CASES)[:4]

    async def always_faithful(q, a, p):
        return 1.0

    r = await calibrate(cases, always_faithful, threshold=0.75)
    assert r["n"] == 4
    assert {d["id"] for d in r["disagreements"]} == {c.id for c in cases if not c.human_faithful}
