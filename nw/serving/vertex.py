"""The Agent Platform (formerly Vertex AI) custom container contract, as routes on a course app.

The contract ("Custom container requirements for inference", docs.cloud.google.com/vertex-ai/
docs/predictions/custom-container-requirements, fetched 2026-09-29): the container listens on
`AIP_HTTP_PORT` (default 8080), answers `GET AIP_HEALTH_ROUTE` (default `/health`) with 200
when it can serve, and `POST AIP_PREDICT_ROUTE` (default `/predict`) with a body of
`{"instances": [...], "parameters": {...}}` answered by `{"predictions": [...]}`. The platform
sets the three variables on the container; the defaults here are the platform's defaults so a
local run of the same image answers the same paths.

`install_vertex_routes` adds the two routes beside the service's own (`/triage`, `/classify`,
the probes, `/metrics`) so one image serves the live Vertex endpoint and a tenant's Cloud Run
service alike. The health route is a probe and stays open like `/healthz`. The predict route is
open only when the process runs under the platform (`AIP_HTTP_PORT` set): the endpoint has
already authenticated the caller with IAM, and the platform sends no `x-api-key`. Anywhere else
the predict route needs the key like every other route.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from nw import auth

DEFAULT_PORT = 8080
DEFAULT_HEALTH_ROUTE = "/health"
DEFAULT_PREDICT_ROUTE = "/predict"

Predict = Callable[[list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]]


class PredictRequest(BaseModel):
    instances: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


def routes(env: Mapping[str, str] | None = None) -> tuple[str, str, int]:
    """`(health_route, predict_route, port)` as the platform configured them."""
    e = os.environ if env is None else env
    health = e.get("AIP_HEALTH_ROUTE") or DEFAULT_HEALTH_ROUTE
    predict = e.get("AIP_PREDICT_ROUTE") or DEFAULT_PREDICT_ROUTE
    port = int(e.get("AIP_HTTP_PORT") or DEFAULT_PORT)
    return health, predict, port


def under_platform(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return bool(e.get("AIP_HTTP_PORT"))


def install_vertex_routes(
    app: FastAPI,
    predict: Predict,
    *,
    ready: Callable[[], bool],
    env: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Register the health and predict routes and return their paths (to keep them out of the
    `/v1` mirror: the platform addresses them by the configured path, not by API version)."""
    health_route, predict_route, _ = routes(env)
    auth.OPEN_PATHS.add(health_route)
    if under_platform(env):
        auth.OPEN_PATHS.add(predict_route)

    @app.get(health_route, tags=["vertex"], name="vertex_health", include_in_schema=True)
    def vertex_health() -> JSONResponse:
        if not ready():
            return JSONResponse({"status": "loading"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post(predict_route, tags=["vertex"], name="vertex_predict")
    def vertex_predict(req: PredictRequest) -> dict[str, Any]:
        if not ready():
            raise HTTPException(503, "model not loaded")
        try:
            predictions = predict(req.instances, req.parameters)
        except ValidationError as exc:
            raise HTTPException(400, exc.errors()) from exc
        return {"predictions": predictions}

    return frozenset({health_route, predict_route})
