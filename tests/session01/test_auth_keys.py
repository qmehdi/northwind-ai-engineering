"""NW_API_KEY holds one secret or a key map; the key id is what leaves the service."""

import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from nw.auth import (
    DEFAULT_KEY_ID,
    install_api_key,
    load_api_keys,
    parse_api_keys,
    resolve_key_id,
    service_client,
)

pytestmark = pytest.mark.session01

KEYS = {"cohort-a": "alpha-secret-1234", "ops": "ops-secret-5678"}


def _count(key_id: str) -> float:
    return REGISTRY.get_sample_value("nw_requests_by_key_total", {"key_id": key_id}) or 0.0


def make(key: str):
    app = FastAPI()
    install_api_key(app, key=key)

    @app.post("/work")
    def work(request: Request):
        return {"key_id": request.state.api_key_id}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return TestClient(app)


def test_parse_accepts_one_secret_or_a_json_map():
    assert parse_api_keys("s3cret") == {DEFAULT_KEY_ID: "s3cret"}
    assert parse_api_keys("  ") == {}
    assert parse_api_keys(json.dumps(KEYS)) == KEYS
    for bad in ("{", "{}", '{"a": 1}', '{"": "x"}', '{"a": ""}', '{"a": {"b": "c"}}'):
        with pytest.raises(ValueError):
            parse_api_keys(bad)


def test_resolve_names_the_matching_key_and_nothing_else():
    assert resolve_key_id("ops-secret-5678", KEYS) == "ops"
    assert resolve_key_id("alpha-secret-1234", KEYS) == "cohort-a"
    assert resolve_key_id("alpha-secret-123", KEYS) is None
    assert resolve_key_id("", KEYS) is None


def test_key_map_from_the_environment_or_the_secret_is_parsed_the_same_way(monkeypatch):
    monkeypatch.setenv("NW_API_KEY", json.dumps(KEYS))
    assert load_api_keys() == KEYS
    monkeypatch.setenv("NW_API_KEY", "single")
    assert load_api_keys() == {DEFAULT_KEY_ID: "single"}


def test_middleware_resolves_the_id_counts_by_it_and_rejects_unknown_keys(caplog):
    c = make(json.dumps(KEYS))
    before = {k: _count(k) for k in ("cohort-a", "ops", "rejected")}
    with caplog.at_level(logging.INFO, logger="nw.auth"):
        assert c.post("/work", headers={"x-api-key": "ops-secret-5678"}).json() == {"key_id": "ops"}
        assert c.post("/work", headers={"x-api-key": "alpha-secret-1234"}).json() == {
            "key_id": "cohort-a"
        }
        assert c.post("/work", headers={"x-api-key": "nope"}).status_code == 401
        assert c.post("/work").status_code == 401
        assert c.get("/healthz").status_code == 200
    assert _count("ops") == before["ops"] + 1
    assert _count("cohort-a") == before["cohort-a"] + 1
    assert _count("rejected") == before["rejected"] + 2
    lines = [r for r in caplog.records if r.getMessage() == "request"]
    assert [r.nw["api_key_id"] for r in lines] == ["ops", "cohort-a", None, None]
    assert [r.nw["status"] for r in lines] == [200, 200, 401, 401]
    assert all("path" in r.nw and "method" in r.nw for r in lines)
    for secret in KEYS.values():
        assert secret not in caplog.text, "the secret never reaches a log line"


def test_single_secret_is_the_default_id_and_no_key_is_none():
    c = make("only")
    assert c.post("/work", headers={"x-api-key": "only"}).json() == {"key_id": DEFAULT_KEY_ID}
    open_client = make("")
    n = _count("none")
    assert open_client.post("/work").json() == {"key_id": None}
    assert _count("none") == n + 1


def test_service_client_sends_the_first_key_of_a_map(monkeypatch):
    monkeypatch.setenv("NW_API_KEY", json.dumps(KEYS))
    assert service_client().headers["x-api-key"] == "alpha-secret-1234"
    monkeypatch.delenv("NW_API_KEY")
    assert "x-api-key" not in service_client().headers
