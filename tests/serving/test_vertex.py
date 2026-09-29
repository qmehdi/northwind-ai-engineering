"""The Agent Platform custom container contract on a course app."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from nw import auth
from nw.serving.vertex import install_vertex_routes, routes, under_platform


class Instance(BaseModel):
    body: str


def _predict(instances, parameters):
    return [{"length": len(Instance.model_validate(i).body), **parameters} for i in instances]


@pytest.fixture
def app_and_state():
    state = {"ready": False}
    app = FastAPI()
    paths = install_vertex_routes(app, _predict, ready=lambda: state["ready"], env={})
    return app, state, paths


def test_defaults_match_the_platform():
    assert routes({}) == ("/health", "/predict", 8080)
    assert routes(
        {"AIP_HEALTH_ROUTE": "/h", "AIP_PREDICT_ROUTE": "/p", "AIP_HTTP_PORT": "9000"}
    ) == (
        "/h",
        "/p",
        9000,
    )
    assert under_platform({"AIP_HTTP_PORT": "8080"}) and not under_platform({})


def test_health_follows_readiness_and_predict_answers_the_contract(app_and_state):
    app, state, paths = app_and_state
    assert paths == frozenset({"/health", "/predict"})
    with TestClient(app) as c:
        assert c.get("/health").status_code == 503
        assert c.post("/predict", json={"instances": [{"body": "x"}]}).status_code == 503
        state["ready"] = True
        assert c.get("/health").json() == {"status": "ok"}
        r = c.post("/predict", json={"instances": [{"body": "abc"}], "parameters": {"k": 1}})
        assert r.status_code == 200 and r.json() == {"predictions": [{"length": 3, "k": 1}]}
        assert c.post("/predict", json={}).json() == {"predictions": []}
        assert c.post("/predict", json={"instances": [{"nope": 1}]}).status_code == 400


def test_predict_route_is_open_only_under_the_platform():
    app = FastAPI()
    install_vertex_routes(app, _predict, ready=lambda: True, env={"AIP_PREDICT_ROUTE": "/keyed"})
    assert "/keyed" not in auth.OPEN_PATHS and "/health" in auth.OPEN_PATHS
    app2 = FastAPI()
    install_vertex_routes(
        app2,
        _predict,
        ready=lambda: True,
        env={"AIP_HTTP_PORT": "8080", "AIP_PREDICT_ROUTE": "/open"},
    )
    assert "/open" in auth.OPEN_PATHS
    auth.OPEN_PATHS.discard("/open")
