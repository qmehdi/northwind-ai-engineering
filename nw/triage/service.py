"""The triage scoring service: FastAPI in front of a versioned model artifact.

    NW_TRIAGE_MODEL=artifacts/triage/latest uv run uvicorn nw.triage.service:app --port 8001

Endpoints:
- POST /triage        score one ticket
- GET  /healthz       process is up (load balancer liveness)
- GET  /readyz        model is loaded and answered a probe (readiness: routable)
- GET  /metrics       Prometheus text format
- GET  /version       model version and metadata
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.triage.model import TriageModel, TriageResult

log = get_logger("nw.triage.service")

REQUESTS = Counter("nw_triage_requests_total", "Triage requests", ["outcome"])
PREDICTIONS = Counter(
    "nw_triage_predictions_total", "Predictions by priority", ["priority", "rule"]
)
LATENCY = Histogram(
    "nw_triage_latency_seconds",
    "Scoring latency",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
)
MODEL_LOADED = Gauge("nw_triage_model_loaded", "1 when a model is loaded")
MODEL_INFO = Gauge("nw_triage_model_info", "Model version as a label", ["version"])


class TicketIn(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class State:
    model: TriageModel | None = None
    ready: bool = False


state = State()


def load_model(path: Path) -> TriageModel:
    model = TriageModel.load(path)
    # Probe once so readiness means "can actually score", not "file was found".
    model.predict([{"subject": "probe", "body": "readiness probe"}])
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    path = Path(os.environ.get("NW_TRIAGE_MODEL", "artifacts/triage/latest"))
    state.model, state.ready = load_model(path), True  # Step 7: and if it fails?
    yield
    state.model, state.ready = None, False
    MODEL_LOADED.set(0)


app = FastAPI(title="Northwind triage", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def correlation(request: Request, call_next):
    cid = request.headers.get("x-correlation-id")
    with bind_correlation_id(cid) as bound:
        response = await call_next(request)
        response.headers["x-correlation-id"] = bound
        return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    if not state.ready or state.model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return {"status": "ready", "model_version": state.model.version}


@app.get("/version")
def version() -> dict:
    if state.model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    meta = {k: v for k, v in state.model.metadata.items() if k != "metrics"}
    return {
        "model_version": state.model.version,
        "p0_threshold": state.model.p0_threshold,
        "metadata": meta,
    }


@app.post("/triage", response_model=TriageResult)
def triage(ticket: TicketIn) -> TriageResult:
    if state.model is None:
        REQUESTS.labels(outcome="not_ready").inc()
        raise HTTPException(status_code=503, detail="model not loaded")
    started = time.perf_counter()
    result = state.model.predict([ticket.model_dump()])[0]
    LATENCY.observe(time.perf_counter() - started)
    REQUESTS.labels(outcome="ok").inc()
    PREDICTIONS.labels(priority=result.priority, rule=result.rule).inc()
    log.info(
        "triage",
        extra=log_fields(
            priority=result.priority,
            confidence=result.confidence,
            rule=result.rule,
            model_version=result.model_version,
        ),
    )
    return result


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
