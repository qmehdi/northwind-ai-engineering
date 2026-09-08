"""Acceptance 5: the service distinguishes liveness from readiness, exposes metrics,
and answers with a versioned, explained prediction."""

import pytest
from fastapi.testclient import TestClient

from nw.triage import service

pytestmark = pytest.mark.session02


@pytest.fixture
def client(trained, monkeypatch):
    model, _, out = trained
    monkeypatch.setenv("NW_TRIAGE_MODEL", str(out / "latest"))
    with TestClient(service.app) as c:
        yield c


@pytest.fixture
def broken_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NW_TRIAGE_MODEL", str(tmp_path / "missing"))
    with TestClient(service.app) as c:
        yield c


def test_healthz_and_readyz(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["model_version"]


def test_missing_model_is_alive_but_not_ready(broken_client):
    assert broken_client.get("/healthz").status_code == 200
    assert broken_client.get("/readyz").status_code == 503
    assert broken_client.post("/triage", json={"subject": "x", "body": "y"}).status_code == 503


def test_triage_returns_priority_probabilities_and_version(client):
    r = client.post(
        "/triage",
        json={
            "subject": "URGENT production down",
            "body": "All users get 502 errors, production is blocked, critical outage since 7am.",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["priority"] == "P0"
    assert set(body["probabilities"]) == {"P0", "P1", "P2", "P3"}
    assert abs(sum(body["probabilities"].values()) - 1.0) < 0.01
    assert body["model_version"] and body["rule"]


def test_correlation_id_round_trips(client):
    r = client.post(
        "/triage",
        json={"subject": "q", "body": "How do I export the audit log?"},
        headers={"x-correlation-id": "abc123"},
    )
    assert r.headers["x-correlation-id"] == "abc123"
    r2 = client.get("/healthz")
    assert r2.headers["x-correlation-id"]


def test_metrics_endpoint_counts_predictions(client):
    client.post(
        "/triage",
        json={"subject": "q", "body": "Is there documentation on exporting the audit log?"},
    )
    text = client.get("/metrics").text
    assert "nw_triage_requests_total" in text
    assert "nw_triage_predictions_total" in text
    assert "nw_triage_latency_seconds_bucket" in text
    assert "nw_triage_model_info" in text


def test_validation_rejects_empty_body(client):
    assert client.post("/triage", json={"subject": "x", "body": ""}).status_code == 422
