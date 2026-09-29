"""Project 1 on the platform: the model from NW_MODEL_URI, the version fields, the tenant on
every drift alert, and the Agent Platform contract beside the course routes."""

import logging

import pytest
from fastapi.testclient import TestClient

from nw.triage import service

pytestmark = pytest.mark.session02


@pytest.fixture
def platform_client(trained, monkeypatch):
    _, _, out = trained
    monkeypatch.setenv("NW_TRIAGE_MODEL", "artifacts/does-not-exist")
    monkeypatch.setenv("NW_MODEL_URI", (out / "latest").as_uri())
    monkeypatch.setenv("NW_MODEL_VERSION", "42")
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.setenv("NW_ENVIRONMENT", "northwind")
    monkeypatch.setenv("NW_TRIAGE_DRIFT_MIN", "5")
    monkeypatch.setenv("NW_TRIAGE_DRIFT_EVERY", "5")
    with TestClient(service.app) as c:
        yield c


def test_model_loads_from_the_uri_and_version_reports_the_platform_fields(platform_client, trained):
    model, _, out = trained
    assert platform_client.get("/readyz").status_code == 200
    v = platform_client.get("/version").json()
    assert v["model_version"] == "42" and v["artifact_version"] == model.version
    assert v["model_uri"] == (out / "latest").as_uri()
    assert v["tenant"] == "alice" and v["environment"] == "northwind" and v["config_hash"]
    assert service.state.source is not None and service.state.source.fetched


def test_drift_alert_carries_the_tenant(platform_client, caplog):
    body = "x" * 15000
    with caplog.at_level(logging.WARNING, logger="nw.triage.service"):
        for _ in range(10):
            assert (
                platform_client.post("/triage", json={"subject": "s", "body": body}).status_code
                == 200
            )
    alerts = [r for r in caplog.records if r.getMessage() == "drift_alert"]
    assert alerts, "ten fifteen-thousand-character tickets must trip the text length PSI"
    assert alerts[-1].nw["tenant"] == "alice" and alerts[-1].nw["environment"] == "northwind"
    assert platform_client.get("/drift").json()["level"] == "alert"


def test_vertex_routes_answer_the_contract_beside_the_course_routes(platform_client):
    assert platform_client.get("/health").json() == {"status": "ok"}
    r = platform_client.post(
        "/predict",
        json={
            "instances": [
                {"subject": "URGENT production down", "body": "All users get 502 errors."},
                {"subject": "q", "body": "How do I export the audit log?"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    preds = r.json()["predictions"]
    assert len(preds) == 2 and preds[0]["priority"] == "P0" and preds[0]["model_version"]
    assert (
        platform_client.post(
            "/predict", json={"instances": [{"subject": "x", "body": ""}]}
        ).status_code
        == 400
    )
    assert platform_client.post("/v1/predict", json={"instances": []}).status_code == 404
    assert platform_client.post("/v1/triage", json={"subject": "s", "body": "b"}).status_code == 200


def test_bad_uri_leaves_the_service_alive_but_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("NW_MODEL_URI", (tmp_path / "missing").as_uri())
    with TestClient(service.app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503
        assert c.get("/health").status_code == 503
        assert c.get("/version").status_code == 503
    assert service.state.source is not None and service.state.source.error
