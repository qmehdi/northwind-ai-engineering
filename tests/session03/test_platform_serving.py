"""Project 2 on the platform: the artifact from NW_MODEL_URI, the version fields and the
Agent Platform contract on the semantic app."""

import pytest
from fastapi.testclient import TestClient

from nw.semantic import service

pytestmark = pytest.mark.session03


@pytest.fixture
def platform_client(exported_tiny, monkeypatch):
    out, _ = exported_tiny
    monkeypatch.setenv("NW_SEMANTIC_ARTIFACT", "artifacts/does-not-exist")
    monkeypatch.setenv("NW_MODEL_URI", out.as_uri())
    monkeypatch.setenv("NW_MODEL_VERSION", "3")
    monkeypatch.setenv("NW_TENANT", "bob")
    monkeypatch.delenv("NW_INDEX", raising=False)
    with TestClient(service.app) as c:
        yield c


def test_artifact_loads_from_the_uri_and_version_reports_the_platform_fields(
    platform_client, exported_tiny
):
    out, _ = exported_tiny
    assert platform_client.get("/readyz").json()["format"] == "model.int8.onnx"
    v = platform_client.get("/version").json()
    assert v["model_version"] == "3" and v["artifact_version"] == service.state.version
    assert (
        v["model_uri"] == out.as_uri() and v["tenant"] == "bob" and v["environment"] == "northwind"
    )


def test_vertex_predict_classifies_every_instance(platform_client):
    assert platform_client.get("/health").json() == {"status": "ok"}
    r = platform_client.post(
        "/predict",
        json={"instances": [{"subject": "production down", "body": "outage all users", "k": 0}]},
    )
    assert r.status_code == 200, r.text
    pred = r.json()["predictions"][0]
    assert pred["priority"] in {"P0", "P1", "P2", "P3"} and pred["similar"] == []
    assert platform_client.post("/v1/predict", json={"instances": []}).status_code == 404
