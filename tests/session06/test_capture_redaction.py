"""Every capture file and the feedback log are redacted before a line is written: a
backtest file never holds a raw email, phone number or account id."""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from nw.agent.monitor import RunSummary
from nw.policy.redact import redact, redact_fields

pytestmark = pytest.mark.session06

EMAIL = "anna.keller@bluefreight.example"
PHONE = "+49 30 1234 5678"
ACCOUNT = "NW-10000"
TEXT = f"Please call {PHONE} or write to {EMAIL} about account {ACCOUNT}."
RAW = (EMAIL, PHONE, ACCOUNT)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _clean(record: dict) -> None:
    dumped = json.dumps(record)
    for raw in RAW:
        assert raw not in dumped, f"{raw} reached the capture file"
    assert "[EMAIL_1]" in dumped


def test_redact_fields_touches_only_the_named_strings():
    out = redact_fields({"body": TEXT, "n": 3, "note": "", "keep": EMAIL}, "body", "note", "x")
    assert out["n"] == 3 and out["note"] == "" and out["keep"] == EMAIL
    assert EMAIL not in out["body"] and out["body"] == redact(TEXT).text


def test_triage_capture_is_redacted(tmp_path):
    from nw.triage import service
    from nw.triage.model import TriageResult

    service.state.capture = tmp_path / "predictions.jsonl"
    result = TriageResult(
        priority="P1", probabilities={"P1": 1.0}, confidence=1.0, rule="model", model_version="v"
    )
    service._capture(service.TicketIn(subject=f"From {EMAIL}", body=TEXT), result, None)
    (record,) = _lines(service.state.capture)
    _clean(record)
    assert record["priority"] == "P1" and record["shadow_priority"] is None
    service.state.capture = None


def test_semantic_capture_is_redacted(tmp_path):
    from nw.semantic import service

    service.state.capture = tmp_path / "predictions.jsonl"
    result = service.Classification(
        tags=["Login"],
        tag_scores={"Login": 0.9},
        priority="P2",
        priority_scores={"P2": 0.9},
        similar=[],
        model_version="v",
        format="model.int8.onnx",
    )
    service._capture(service.TicketIn(subject="Login", body=TEXT), result, "P2")
    (record,) = _lines(service.state.capture)
    _clean(record)
    assert record["tags"] == ["Login"]
    service.state.capture = None


def test_policy_capture_and_feedback_are_redacted(tmp_path, monkeypatch):
    from nw.policy import service

    @asynccontextmanager
    async def noop(app):
        yield

    monkeypatch.setattr(service, "state", service.State())
    service.state.capture = tmp_path / "capture.jsonl"
    service.state.feedback_path = tmp_path / "feedback.jsonl"
    req = service.Ask(question=f"Can {EMAIL} at {ACCOUNT} get a refund? Call {PHONE}.")
    resp = service.AskResponse(
        text=f"Yes, {EMAIL} is eligible.",
        citations=["refunds-2025#1"],
        confidence=0.9,
        refused=False,
        reason=None,
        context_ids=["refunds-2025#1"],
        prompt_version="policy.answer@abc",
        answer_id="a" * 32,
        model_id="fake",
    )
    service._capture(resp, req, 0.9, 12.0)
    (record,) = _lines(service.state.capture)
    _clean(record)
    service._remember(resp, req, 0.9)
    service.app.router.lifespan_context = noop
    with TestClient(service.app) as c:
        r = c.post(
            "/v1/feedback",
            json={
                "answer_id": resp.answer_id,
                "verdict": "wrong",
                "note": f"Told {EMAIL} the wrong thing",
            },
        )
    assert r.status_code == 200 and r.json()["recorded"] is True
    (fb,) = _lines(service.state.feedback_path)
    _clean(fb)
    assert fb["verdict"] == "wrong" and "[EMAIL_1]" in fb["note"] and "[EMAIL_1]" in fb["text"]


def test_agent_capture_passes_through_redaction(tmp_path):
    from nw.agent import service

    service.state.capture = tmp_path / "runs.jsonl"
    summary = RunSummary(
        run_id="r1",
        agent=f"resolver for {EMAIL}",
        agent_version="abc",
        terminated="answer",
        steps=2,
        cost_usd=0.01,
        tool_calls=1,
        tool_errors=0,
        proposals=0,
    )
    service._capture(summary)
    (record,) = _lines(service.state.capture)
    assert EMAIL not in json.dumps(record) and record["steps"] == 2
    service.state.capture = None
