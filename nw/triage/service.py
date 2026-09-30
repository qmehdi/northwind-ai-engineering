"""The triage scoring service: FastAPI in front of a versioned model artifact.

    NW_TRIAGE_MODEL=artifacts/triage/latest uv run uvicorn nw.triage.service:app --port 8001

Endpoints:
- POST /triage        score one ticket
- GET  /healthz       process is up (load balancer liveness)
- GET  /readyz        model is loaded and answered a probe (readiness: routable)
- GET  /metrics       Prometheus text format
- GET  /version       model version and metadata
- GET  /drift         input and prediction drift against the training profile

Environment, all optional: NW_TRIAGE_SHADOW_MODEL (a second artifact scored on every request,
agreement counted, never served), NW_TRIAGE_CAPTURE (a JSONL file of requests and predictions
for backtests), NW_TRIAGE_DRIFT_WINDOW, NW_TRIAGE_DRIFT_MIN, NW_TRIAGE_DRIFT_EVERY.

On a platform (ADR 0008) the model comes from the registry: `NW_MODEL_URI` (`s3://`, `gs://`,
`file://` or MLflow `models:/`) is fetched into a temp directory at startup and
`NW_MODEL_VERSION` names the version served; `NW_TRIAGE_MODEL` stays the fallback. `/version`
reports both with the tenant and the environment, which every `drift_alert` line carries too.
The same app answers the Agent Platform's custom container contract (`AIP_HEALTH_ROUTE`,
`AIP_PREDICT_ROUTE`, `{"instances": [...]}` in, `{"predictions": [...]}` out) for the live
Vertex endpoint.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from nw.api import UNVERSIONED, install_version_headers, mount_versioned, version_fields
from nw.auth import install_api_key
from nw.config import settings
from nw.logging import (
    bind_correlation_id,
    configure_logging,
    correlation_id,
    get_logger,
    log_fields,
)
from nw.metrics_export import start_metrics_export
from nw.policy.redact import redact_fields
from nw.ratelimit import install_rate_limit
from nw.serving.download import ModelSource, model_source
from nw.serving.identity import bind_identity, identity
from nw.serving.vertex import install_vertex_routes
from nw.telemetry import configure_tracing, instrument_app
from nw.triage.model import TriageModel, TriageResult
from nw.triage.monitor import ALERT, DriftMonitor

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
DRIFT = Gauge("nw_triage_drift_psi", "Population stability index against training", ["feature"])
DRIFT_LEVEL = Gauge("nw_triage_drift_level", "0 ok, 1 watch, 2 alert, -1 warming up")
SHADOW = Counter("nw_triage_shadow_total", "Shadow model comparisons", ["agree"])


class TicketIn(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    # The helpdesk's id, when the caller has one: capture lines carry it so the online quality
    # job (`nw.triage.monitor quality`) can join a prediction with the label that comes later.
    ticket_id: str | None = Field(default=None, pattern=r"^T-\d{6}$")


class State:
    model: TriageModel | None = None
    shadow: TriageModel | None = None
    ready: bool = False
    monitor: DriftMonitor = DriftMonitor(None)
    drift_every: int = 50
    seen: int = 0
    capture: Path | None = None
    source: ModelSource | None = None  # where the served artifact came from


state = State()


def load_model(path: Path, expected_sha256: str | None = None) -> TriageModel:
    """`expected_sha256` is the registry's hash of the model file when the platform injected
    one; without it the load checks the hash the training run wrote into the metadata."""
    model = TriageModel.load(path, expected_sha256=expected_sha256)
    # Probe once so readiness means "can actually score", not "file was found".
    model.predict([{"subject": "probe", "body": "readiness probe"}])
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    bind_identity()
    start_metrics_export("triage")
    # The registry's artifact when the platform injected NW_MODEL_URI, else the local path.
    state.source = model_source(
        fallback=Path(os.environ.get("NW_TRIAGE_MODEL", "artifacts/triage/latest"))
    )
    path = state.source.path
    state.model, state.ready = load_model(path), True  # readiness: if it fails?
    _configure_mlops(path)
    yield
    state.model, state.ready = None, False
    MODEL_LOADED.set(0)


def _configure_mlops(path: Path) -> None:
    """Drift baseline from the artifact's data profile, an optional shadow model, and an
    optional prediction capture file. All three are best effort: the service scores
    without them."""
    profile_path = path / "data_profile.json"
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else None
    state.monitor = DriftMonitor(
        profile,
        window=int(os.environ.get("NW_TRIAGE_DRIFT_WINDOW", "500")),
        min_window=int(os.environ.get("NW_TRIAGE_DRIFT_MIN", "200")),
    )
    state.drift_every = int(os.environ.get("NW_TRIAGE_DRIFT_EVERY", "50"))
    state.seen = 0
    if not state.monitor.enabled:
        log.info("drift monitoring off: no data_profile.json in the artifact")
    shadow_path = os.environ.get("NW_TRIAGE_SHADOW_MODEL")
    state.shadow = None
    if shadow_path:
        try:
            state.shadow = load_model(Path(shadow_path))
            log.info("shadow model loaded", extra=log_fields(version=state.shadow.version))
        except Exception as exc:  # noqa: BLE001
            log.error(
                "shadow model failed to load", extra=log_fields(path=shadow_path, error=str(exc))
            )
    capture = os.environ.get("NW_TRIAGE_CAPTURE")
    state.capture = Path(capture) if capture else None


app = FastAPI(title="Northwind triage", version="1.0", lifespan=lifespan)
# Starlette runs the last-added middleware first: the key check runs before the limiter
# (which buckets by key id) and the version headers land on their 401 and 429 too.
install_rate_limit(app)
install_api_key(app)
install_version_headers(app)
configure_tracing("northwind-triage")
instrument_app(app)


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
    source = state.source
    return {
        # The registry's version when the platform pinned one, else the artifact's own.
        "model_version": (source.version if source and source.version else state.model.version),
        "artifact_version": state.model.version,
        "model_uri": source.uri if source else None,
        **identity(),
        "p0_threshold": state.model.p0_threshold,
        "metadata": meta,
        "shadow_version": state.shadow.version if state.shadow else None,
        "config_hash": settings().config_hash(),
        **version_fields(),
    }


@app.get("/drift")
def drift() -> dict:
    """The current drift snapshot: PSI of text length and predicted priority against training."""
    return state.monitor.snapshot().as_dict()


def _observe(ticket: TicketIn, result: TriageResult) -> None:
    state.monitor.observe(len(ticket.subject) + len(ticket.body), result.priority)
    state.seen += 1
    if state.seen % state.drift_every == 0:
        snap = state.monitor.snapshot()
        levels = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}
        DRIFT_LEVEL.set(levels[snap.level])
        if snap.text_length_psi is not None:
            DRIFT.labels(feature="text_length").set(snap.text_length_psi)
            DRIFT.labels(feature="priority").set(snap.priority_psi or 0.0)
        if snap.level == "alert":
            log.warning(
                "drift_alert",
                extra=log_fields(
                    text_length_psi=snap.text_length_psi,
                    priority_psi=snap.priority_psi,
                    window=snap.window,
                    threshold=ALERT,
                    model_version=state.model.version if state.model else None,
                    **identity(),
                ),
            )


def _shadow(ticket: TicketIn, result: TriageResult) -> str | None:
    if state.shadow is None:
        return None
    other = state.shadow.predict([ticket.model_dump()])[0]
    SHADOW.labels(agree=str(other.priority == result.priority).lower()).inc()
    if other.priority != result.priority:
        log.info(
            "shadow_disagreement",
            extra=log_fields(
                served=result.priority, shadow=other.priority, shadow_version=other.model_version
            ),
        )
    return other.priority


def _capture(ticket: TicketIn, result: TriageResult, shadow: str | None) -> None:
    if state.capture is None:
        return
    record = {
        "ts": time.time(),
        "ticket_id": ticket.ticket_id,
        "correlation_id": correlation_id(),
        "subject": ticket.subject,
        "body": ticket.body,
        "priority": result.priority,
        "confidence": result.confidence,
        "rule": result.rule,
        "model_version": result.model_version,
        "shadow_priority": shadow,
    }
    record = redact_fields(record, "subject", "body")
    state.capture.parent.mkdir(parents=True, exist_ok=True)
    with state.capture.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def score(ticket: TicketIn) -> TriageResult:
    """One ticket through the model with every side effect: metrics, drift window, shadow,
    capture and the log line. Both the course route and the platform route call it."""
    if state.model is None:
        REQUESTS.labels(outcome="not_ready").inc()
        raise HTTPException(status_code=503, detail="model not loaded")
    started = time.perf_counter()
    result = state.model.predict([ticket.model_dump()])[0]
    LATENCY.observe(time.perf_counter() - started)
    REQUESTS.labels(outcome="ok").inc()
    PREDICTIONS.labels(priority=result.priority, rule=result.rule).inc()
    _observe(ticket, result)
    shadow = _shadow(ticket, result)
    if shadow is not None:
        state.monitor.observe_shadow(shadow == result.priority)
    _capture(ticket, result, shadow)
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


@app.post("/triage", response_model=TriageResult)
def triage(ticket: TicketIn) -> TriageResult:
    return score(ticket)


def vertex_predict(instances: list[dict], parameters: dict) -> list[dict]:
    """The Agent Platform contract: every instance is a ticket, every prediction a result."""
    return [score(TicketIn.model_validate(i)).model_dump() for i in instances]


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


VERTEX_ROUTES = install_vertex_routes(app, vertex_predict, ready=lambda: state.ready)
# /v1/... is the API; the bare paths are deprecated aliases for one release. The platform's
# routes are addressed by their configured path and stay out of the mirror.
mount_versioned(app, exclude=UNVERSIONED | VERTEX_ROUTES)
