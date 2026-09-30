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
    platform = {"AIP_MODE": "PREDICTION", "AIP_ENDPOINT_ID": "1", "AIP_DEPLOYED_MODEL_ID": "2"}
    assert under_platform(platform) and not under_platform({})
    # The image bakes AIP_HTTP_PORT in, so it is no signal at all.
    assert not under_platform({"AIP_HTTP_PORT": "8080", "AIP_MODE": "PREDICTION"})


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


PLATFORM = {
    "AIP_MODE": "PREDICTION",
    "AIP_ENDPOINT_ID": "123",
    "AIP_DEPLOYED_MODEL_ID": "456",
}


def _keyed_app(env, *, rps=None):
    from nw.ratelimit import install_rate_limit

    app = FastAPI()
    install_rate_limit(app, rps=rps or 100.0, burst=2)
    auth.install_api_key(app, key="s3cret")
    install_vertex_routes(app, _predict, ready=lambda: True, env=env)
    return app


def test_predict_route_is_open_only_under_the_platform_and_only_on_this_app():
    app = FastAPI()
    install_vertex_routes(app, _predict, ready=lambda: True, env={"AIP_PREDICT_ROUTE": "/keyed"})
    assert "/keyed" not in auth.open_paths(app) and "/health" in auth.open_paths(app)
    app2 = FastAPI()
    install_vertex_routes(
        app2, _predict, ready=lambda: True, env={**PLATFORM, "AIP_PREDICT_ROUTE": "/open"}
    )
    assert "/open" in auth.open_paths(app2)
    assert "/open" not in auth.OPEN_PATHS and "/open" not in auth.open_paths(app)


def test_cloud_run_image_keeps_the_key_on_predict():
    # What the serving image looks like on Cloud Run or Container Apps: AIP_* baked, no endpoint.
    env = {"AIP_HTTP_PORT": "8080", "AIP_HEALTH_ROUTE": "/health", "AIP_PREDICT_ROUTE": "/predict"}
    with TestClient(_keyed_app(env)) as c:
        body = {"instances": [{"body": "x"}]}
        assert c.post("/predict", json=body).status_code == 401
        ok = c.post("/predict", json=body, headers={"x-api-key": "s3cret"})
        assert ok.status_code == 200
        assert c.get("/health").status_code == 200


def test_platform_predict_is_open_but_still_rate_limited():
    with TestClient(_keyed_app(PLATFORM, rps=0.001)) as c:
        body = {"instances": [{"body": "x"}]}
        codes = [c.post("/predict", json=body).status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and codes[2:] == [429, 429]
        assert all(c.get("/health").status_code == 200 for _ in range(5)), "probes never wait"
