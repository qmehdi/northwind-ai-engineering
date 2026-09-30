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


def test_no_key_off_the_local_track_fails_closed(monkeypatch):
    monkeypatch.setenv("NW_TRACK", "aws")
    monkeypatch.delenv("NW_AUTH_DISABLED", raising=False)
    c = make(monkeypatch, None)
    assert c.get("/healthz").status_code == 200, "probes stay open so the platform sees it"
    r = c.post("/work")
    assert r.status_code == 503 and "NW_API_KEY" in r.json()["detail"]


def test_auth_disabled_is_an_explicit_choice(monkeypatch):
    monkeypatch.setenv("NW_TRACK", "gcp")
    monkeypatch.setenv("NW_AUTH_DISABLED", "1")
    c = make(monkeypatch, None)
    assert c.post("/work").status_code == 200


def test_secret_references_resolve_in_order_and_key_vault_uris_parse():
    from nw.secrets import key_vault_parts, read_secret

    seen = []

    def fetch(kind):
        return lambda ref: seen.append((kind, ref)) or f"from-{kind}"

    kw = {"fetch_aws": fetch("aws"), "fetch_gcp": fetch("gcp"), "fetch_azure": fetch("azure")}
    assert read_secret("X", {"X": "plain"}, **kw).source == "env"
    uri = "https://nw-kv.vault.azure.net/secrets/api-key"
    got = read_secret("X", {"X_SECRET_URI": uri}, **kw)
    assert got.value == "from-azure" and got.source == "key-vault" and seen == [("azure", uri)]
    assert read_secret("X", {"X_SECRET_ARN": "arn:a", "X_SECRET_URI": uri}, **kw).source == (
        "secrets-manager"
    )
    assert read_secret("X", {}, **kw).value == ""
    assert key_vault_parts(uri + "/abc123") == (
        "https://nw-kv.vault.azure.net",
        "api-key",
        "abc123",
    )
    with pytest.raises(ValueError):
        key_vault_parts("https://evil.example/secrets/api-key")
