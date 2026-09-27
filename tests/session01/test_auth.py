"""Every service is protected by the same middleware: key required when set, probes open."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nw.auth import install_api_key

pytestmark = pytest.mark.session01


def make(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("NW_API_KEY", raising=False)
    else:
        monkeypatch.setenv("NW_API_KEY", key)
    app = FastAPI()
    install_api_key(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/work")
    def work():
        return {"did": "work"}

    return TestClient(app)


def test_open_when_no_key_configured(monkeypatch):
    c = make(monkeypatch, None)
    assert c.post("/work").status_code == 200


def test_key_required_and_probes_open(monkeypatch):
    c = make(monkeypatch, "s3cret")
    assert c.get("/healthz").status_code == 200
    assert c.post("/work").status_code == 401
    assert c.post("/work", headers={"x-api-key": "wrong"}).status_code == 401
    assert c.post("/work", headers={"x-api-key": "s3cret"}).status_code == 200
