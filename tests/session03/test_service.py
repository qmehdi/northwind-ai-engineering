"""Acceptance: the semantic service runs on the ONNX artifact without torch in the
request path, distinguishes readiness, and returns tags, priority and a version."""

import pytest
from fastapi.testclient import TestClient

from nw.semantic import service

pytestmark = pytest.mark.session03


@pytest.fixture(scope="session")
def artifact(trained_tiny, config, tiny_tokenizer):
    from nw.semantic.export import export_onnx, load_finetuned, quantize

    out, _ = trained_tiny
    model, tok, meta = load_finetuned(out, config=config, tokenizer=tiny_tokenizer)
    fp32 = export_onnx(model, tok, out / "model.onnx", meta["max_length"])
    quantize(fp32, out / "model.int8.onnx")
    tok.save_pretrained(out / "tokenizer")
    return out


@pytest.fixture
def client(artifact, monkeypatch):
    monkeypatch.setenv("NW_SEMANTIC_ARTIFACT", str(artifact))
    monkeypatch.delenv("NW_INDEX", raising=False)
    with TestClient(service.app) as c:
        yield c


def test_ready_reports_quantized_format(client):
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["format"] == "model.int8.onnx"


def test_classify_returns_tags_priority_and_version(client):
    r = client.post(
        "/classify",
        json={"subject": "production down", "body": "outage all users critical error 502", "k": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["priority"] in {"P0", "P1", "P2", "P3"}
    assert abs(sum(body["priority_scores"].values()) - 1) < 0.01
    assert isinstance(body["tags"], list) and body["model_version"]
    assert body["similar"] == []


def test_missing_artifact_is_alive_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("NW_SEMANTIC_ARTIFACT", str(tmp_path))
    with TestClient(service.app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503


def test_metrics_present(client):
    client.post("/classify", json={"body": "slow dashboard", "k": 0})
    assert "nw_semantic_requests_total" in client.get("/metrics").text
