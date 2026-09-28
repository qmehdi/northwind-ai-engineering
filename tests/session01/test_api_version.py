"""Every service answers under /v1, keeps the bare paths as deprecated aliases, and stamps
the API and package versions on every response."""

import importlib
from importlib.metadata import version as installed_version

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nw
from nw.api import (
    API_VERSION,
    deprecated_paths,
    install_version_headers,
    mount_versioned,
    version_fields,
    versioned_paths,
)
from nw.auth import install_api_key

pytestmark = pytest.mark.session01


def make(key=None):
    app = FastAPI()
    install_api_key(app, key=key or "")
    install_version_headers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/work", tags=["work"])
    def work() -> dict[str, str]:
        return {"did": "work"}

    @app.get("/version")
    def version():
        return {"model_version": "m1", **version_fields()}

    added = mount_versioned(app)
    return app, added


def test_package_version_comes_from_the_installed_metadata():
    assert nw.__version__ == installed_version("nw")
    assert nw.__version__ != nw.FALLBACK_VERSION


def test_routes_are_mounted_under_v1_and_the_aliases_are_deprecated():
    app, added = make()
    assert added == ["/v1/work", "/v1/version"], "probes are never versioned"
    assert versioned_paths(app) == ["/v1/work", "/v1/version"]
    assert deprecated_paths(app) == ["/work", "/version"]
    assert mount_versioned(app) == [], "idempotent"
    c = TestClient(app)
    assert c.post("/v1/work").json() == {"did": "work"}
    assert c.post("/work").json() == {"did": "work"}, "the alias still answers"
    spec = app.openapi()
    assert spec["paths"]["/work"]["post"]["deprecated"] is True
    assert "deprecated" in spec["paths"]["/work"]["post"]["tags"]
    assert spec["paths"]["/v1/work"]["post"].get("deprecated") is None
    assert spec["paths"]["/v1/work"]["post"]["tags"] == ["work"]
    assert "/v1/work" in spec["paths"]["/work"]["post"]["description"]
    assert list(spec["paths"]).index("/v1/work") < list(spec["paths"]).index("/work")


def test_every_response_carries_the_version_headers_refusals_included():
    app, _ = make(key="k")
    c = TestClient(app)
    ok = c.post("/v1/work", headers={"x-api-key": "k"})
    refused = c.post("/v1/work")
    probe = c.get("/healthz")
    for r in (ok, refused, probe):
        assert r.headers["x-api-version"] == API_VERSION == "1"
        assert r.headers["x-nw-version"] == nw.__version__
    assert refused.status_code == 401
    assert c.get("/v1/version", headers={"x-api-key": "k"}).json() == {
        "model_version": "m1",
        "api_version": "1",
        "nw_version": nw.__version__,
    }


@pytest.mark.parametrize(
    "module, paths",
    [
        ("nw.triage.service", {"/v1/triage", "/v1/version", "/v1/drift"}),
        ("nw.semantic.service", {"/v1/classify", "/v1/version", "/v1/drift"}),
        ("nw.policy.service", {"/v1/ask", "/v1/feedback", "/v1/version", "/v1/drift"}),
        ("nw.agent.service", {"/v1/run", "/v1/route", "/v1/version", "/v1/drift"}),
    ],
)
def test_the_four_services_expose_versioned_routes_and_open_probes(module, paths):
    app = importlib.import_module(module).app
    versioned = set(versioned_paths(app))
    assert paths <= versioned
    assert versioned.isdisjoint({"/v1/healthz", "/v1/readyz", "/v1/metrics"})
    assert {"/healthz", "/readyz", "/metrics"}.isdisjoint(deprecated_paths(app))
