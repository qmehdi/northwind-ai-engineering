"""Redaction, measured: German IBANs and national phone formats, invoices that are not
accounts, names and addresses through the optional detector, the agent path's `keep`, and
span recall and precision on a labelled set (the PII overlay in `data/pii` when it is
there, the small fixture beside this test always)."""

from pathlib import Path

import pytest

from nw.policy.redact import (
    detect,
    detector_from_env,
    evaluate,
    heuristic_detector,
    load_labelled,
    redact,
    redact_for_agent,
)

pytestmark = pytest.mark.session04

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "pii_labelled.jsonl"
OVERLAY = ROOT / "data" / "pii" / "messages.jsonl"
STRUCTURED = ("EMAIL", "PHONE", "IBAN", "CARD", "ACCOUNT", "INVOICE", "IP", "KEY")


def test_german_iban_and_national_phone_formats():
    out = redact("IBAN DE89 3704 0044 0532 0130 00, Tel. 030 23125 456 oder 0171 3920084").text
    assert "[IBAN_1]" in out and out.count("[PHONE_") == 2
    assert "3704" not in out and "0171" not in out
    out = redact("call (312) 555-0110, 415-555-0193 or 555-0174").text
    assert out.count("[PHONE_") == 3


def test_an_invoice_id_is_not_an_account():
    out = redact("Invoice #NW-88214 for account NW-10007; invoice number is NW-77650.")
    assert out.text == (
        "Invoice #[INVOICE_1] for account [ACCOUNT_1]; invoice number is [INVOICE_2]."
    )


def test_dates_numbers_and_ids_survive():
    text = "SLA 99.95 percent since 2025-03-01, 30 days, 15 minutes, sla-2025, T-200001, 10:30"
    assert redact(text).text == text


def test_names_and_addresses_need_the_detector():
    text = "Ship to Fiktivallee 87, 10115 Musterstadt, attention Jonas Kastwald."
    assert redact(text, detector=None).text == text
    out = redact(text, detector=heuristic_detector).text
    assert "Kastwald" not in out and "Fiktivallee" not in out and "[NAME_1]" in out


def test_the_detector_is_chosen_by_environment():
    assert detector_from_env({}) is None
    assert detector_from_env({"NW_REDACT_DETECTOR": "heuristic"}) is heuristic_detector
    with pytest.raises(ValueError):
        detector_from_env({"NW_REDACT_DETECTOR": "magic"})


def test_the_agent_path_keeps_account_and_invoice_ids():
    out = redact_for_agent("Account NW-10007, invoice INV-20481, mail a@b.example")
    assert "NW-10007" in out and "INV-20481" in out and "a@b.example" not in out


def test_spans_do_not_overlap():
    spans = detect("DE89 3704 0044 0532 0130 00 and 4111 1111 1111 1111", detector=None)
    assert [s.kind for s in spans] == ["IBAN", "CARD"]


def test_recall_and_precision_on_the_fixture():
    r = evaluate(load_labelled(FIXTURE), detector=heuristic_detector)
    assert r["recall"] == 1.0 and r["precision"] == 1.0


@pytest.mark.skipif(not OVERLAY.exists(), reason="the PII overlay is not in this checkout")
def test_recall_and_precision_on_the_pii_overlay():
    rows = load_labelled(OVERLAY)
    patterns = evaluate(rows, detector=None)
    for kind in STRUCTURED:
        k = patterns["by_kind"].get(kind)
        if k and k["gold"]:
            assert k["recall"] >= 0.95, (kind, k)
    assert patterns["precision"] >= 0.95
    with_names = evaluate(rows, detector=heuristic_detector)
    assert with_names["recall"] >= 0.95 and with_names["precision"] >= 0.95
    assert with_names["by_kind"]["NAME"]["recall"] >= 0.85
    assert with_names["by_kind"]["ADDRESS"]["recall"] >= 0.9
