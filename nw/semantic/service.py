"""Project 2 as a service: tags, priority and similar tickets from the ONNX model.

    NW_SEMANTIC_ARTIFACT=artifacts/semantic/latest NW_INDEX=artifacts/index \
      uv run uvicorn nw.semantic.service:app --port 8002

No torch in the runtime path: the ONNX graph and the tokenizer are enough,
which is what makes the container small and the cold start short.

Endpoints:
- POST /classify      tags, priority, similar tickets for one ticket
- GET  /healthz       process is up (liveness)
- GET  /readyz        model is loaded and answered a probe (readiness)
- GET  /metrics       Prometheus text format
- GET  /version       model version and metadata
- GET  /drift         text length, predicted priority and tag rate drift against training

`NW_SEMANTIC_ARTIFACT` is a version directory, `latest`, a flat directory holding
`metadata.json`, or the root `artifacts/semantic`, which means `latest`: the served model
is the one the promotion gate chose. Environment, all optional: NW_QUANTIZED (1, the int8
graph), NW_SEMANTIC_CAPTURE (a JSONL file of requests and predictions for backtests),
NW_SEMANTIC_DRIFT_WINDOW, NW_SEMANTIC_DRIFT_MIN, NW_SEMANTIC_DRIFT_EVERY.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from nw.auth import install_api_key
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.semantic.artifacts import resolve
from nw.semantic.data import TAGS
from nw.semantic.monitor import ALERT, SemanticDriftMonitor
from nw.telemetry import configure_tracing, instrument_app
from nw.triage.features import PRIORITIES, ticket_text

log = get_logger("nw.semantic.service")
REQUESTS = Counter("nw_semantic_requests_total", "Requests", ["endpoint", "outcome"])
LATENCY = Histogram(
    "nw_semantic_latency_seconds",
    "Latency",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
)
LOADED = Gauge("nw_semantic_model_loaded", "1 when the model is loaded")
INFO = Gauge("nw_semantic_model_info", "Model version", ["version", "format"])
PREDICTIONS = Counter("nw_semantic_predictions_total", "Predictions by priority", ["priority"])
DRIFT = Gauge("nw_semantic_drift_psi", "Population stability index against training", ["feature"])
DRIFT_LEVEL = Gauge("nw_semantic_drift_level", "0 ok, 1 watch, 2 alert, -1 warming up")


class TicketIn(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    k: int = Field(default=3, ge=0, le=20)


class Classification(BaseModel):
    tags: list[str]
    tag_scores: dict[str, float]
    priority: str
    priority_scores: dict[str, float]
    similar: list[dict]
    model_version: str
    format: str


class State:
    onnx = None
    tokenizer = None
    thresholds: np.ndarray | None = None
    index = None
    embedder = None
    version = "unknown"
    fmt = "none"
    metadata: dict = {}
    ready = False
    monitor: SemanticDriftMonitor = SemanticDriftMonitor(None)
    drift_every: int = 50
    seen: int = 0
    capture: Path | None = None


state = State()


def load_all(artifact: Path, index_dir: Path | None, quantized: bool) -> None:
    from transformers import AutoTokenizer

    from nw.semantic.export import OnnxEncoder

    artifact = resolve(artifact, serve=True)
    meta = json.loads((artifact / "metadata.json").read_text())
    tok_dir = artifact / "tokenizer"
    state.tokenizer = AutoTokenizer.from_pretrained(
        str(tok_dir) if tok_dir.exists() else meta["base"]
    )
    file = (
        "model.int8.onnx" if quantized and (artifact / "model.int8.onnx").exists() else "model.onnx"
    )
    state.onnx = OnnxEncoder(artifact / file, state.tokenizer, meta["max_length"])
    state.thresholds = np.load(artifact / "tag_thresholds.npy")
    state.version, state.fmt = meta["version"], file
    state.metadata = {k: v for k, v in meta.items() if k not in ("metrics", "tags", "history")}
    if index_dir and (index_dir / "tickets.faiss").exists():
        from nw.semantic.embed import Embedder, TicketIndex

        state.index = TicketIndex.load(index_dir)
        state.embedder = Embedder(json.loads((index_dir / "metadata.json").read_text())["embedder"])
    state.onnx.run(["probe"])
    _configure_mlops(artifact)
    state.ready = True


def _configure_mlops(artifact: Path) -> None:
    """Drift baseline from the artifact's data profile and an optional capture file. Both
    are best effort: the service classifies without them."""
    profile_path = artifact / "data_profile.json"
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else None
    state.monitor = SemanticDriftMonitor(
        profile,
        window=int(os.environ.get("NW_SEMANTIC_DRIFT_WINDOW", "500")),
        min_window=int(os.environ.get("NW_SEMANTIC_DRIFT_MIN", "50")),
    )
    state.drift_every = int(os.environ.get("NW_SEMANTIC_DRIFT_EVERY", "50"))
    state.seen = 0
    if not state.monitor.enabled:
        log.info("drift monitoring off: no data_profile.json in the artifact")
    capture = os.environ.get("NW_SEMANTIC_CAPTURE")
    state.capture = Path(capture) if capture else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    artifact = Path(os.environ.get("NW_SEMANTIC_ARTIFACT", "artifacts/semantic"))
    index_dir = Path(os.environ["NW_INDEX"]) if os.environ.get("NW_INDEX") else None
    try:
        load_all(artifact, index_dir, quantized=os.environ.get("NW_QUANTIZED", "1") == "1")
        LOADED.set(1)
        INFO.labels(version=state.version, format=state.fmt).set(1)
        log.info(
            "model loaded",
            extra=log_fields(
                artifact=str(artifact),
                version=state.version,
                format=state.fmt,
                index=bool(state.index),
                drift=state.monitor.enabled,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        state.ready = False
        LOADED.set(0)
        log.error("model failed to load", extra=log_fields(artifact=str(artifact), error=str(exc)))
    yield
    state.ready = False


app = FastAPI(title="Northwind semantic engine", version="1.0", lifespan=lifespan)
install_api_key(app)
configure_tracing("northwind-semantic")
instrument_app(app)


@app.middleware("http")
async def correlation(request: Request, call_next):
    with bind_correlation_id(request.headers.get("x-correlation-id")) as cid:
        response = await call_next(request)
        response.headers["x-correlation-id"] = cid
        return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    if not state.ready:
        raise HTTPException(503, "model not loaded")
    return {"status": "ready", "model_version": state.version, "format": state.fmt}


@app.get("/version")
def version() -> dict:
    if not state.ready:
        raise HTTPException(503, "model not loaded")
    return {"model_version": state.version, "format": state.fmt, "metadata": state.metadata}


@app.get("/drift")
def drift() -> dict:
    """PSI of text length, predicted priority and tag rate against the training profile."""
    return state.monitor.snapshot().as_dict()


def _observe(ticket: TicketIn, priority: str, tags: list[str]) -> None:
    state.monitor.observe(len(ticket.subject) + len(ticket.body), priority, tags)
    state.seen += 1
    if state.seen % state.drift_every == 0:
        snap = state.monitor.snapshot()
        levels = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}
        DRIFT_LEVEL.set(levels[snap.level])
        if snap.text_length_psi is not None:
            DRIFT.labels(feature="text_length").set(snap.text_length_psi)
            DRIFT.labels(feature="priority").set(snap.priority_psi or 0.0)
            DRIFT.labels(feature="tag_rate").set(snap.tag_rate_psi or 0.0)
        if snap.level == "alert":
            log.warning(
                "drift_alert",
                extra=log_fields(
                    text_length_psi=snap.text_length_psi,
                    priority_psi=snap.priority_psi,
                    tag_rate_psi=snap.tag_rate_psi,
                    window=snap.window,
                    threshold=ALERT,
                    model_version=state.version,
                ),
            )


def _capture(ticket: TicketIn, result: Classification) -> None:
    if state.capture is None:
        return
    record = {
        "ts": time.time(),
        "subject": ticket.subject,
        "body": ticket.body,
        "tags": result.tags,
        "priority": result.priority,
        "priority_scores": result.priority_scores,
        "model_version": result.model_version,
        "format": result.format,
    }
    state.capture.parent.mkdir(parents=True, exist_ok=True)
    with state.capture.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@app.post("/classify", response_model=Classification)
def classify(ticket: TicketIn) -> Classification:
    if not state.ready or state.onnx is None or state.thresholds is None:
        REQUESTS.labels(endpoint="classify", outcome="not_ready").inc()
        raise HTTPException(503, "model not loaded")
    t0 = time.perf_counter()
    text = ticket_text(ticket.subject, ticket.body)
    tag_logits, prio_logits, _ = state.onnx.run([text])
    tag_p = 1 / (1 + np.exp(-tag_logits[0]))
    prio_p = np.exp(prio_logits[0] - prio_logits[0].max())
    prio_p /= prio_p.sum()
    tags = [t for t, p, th in zip(TAGS, tag_p, state.thresholds, strict=True) if p >= th]
    similar: list[dict] = []
    if ticket.k and state.index is not None and state.embedder is not None:
        q = state.embedder.encode([text])
        similar = [
            {"ticket_id": i, "score": round(s, 4), **m}
            for i, s, m in state.index.search(q, ticket.k)[0]
        ]
    LATENCY.labels(endpoint="classify").observe(time.perf_counter() - t0)
    REQUESTS.labels(endpoint="classify", outcome="ok").inc()
    result = Classification(
        tags=tags,
        tag_scores={t: round(float(p), 4) for t, p in zip(TAGS, tag_p, strict=True) if p >= 0.05},
        priority=PRIORITIES[int(prio_p.argmax())],
        priority_scores={p: round(float(v), 4) for p, v in zip(PRIORITIES, prio_p, strict=True)},
        similar=similar,
        model_version=state.version,
        format=state.fmt,
    )
    PREDICTIONS.labels(priority=result.priority).inc()
    _observe(ticket, result.priority, tags)
    _capture(ticket, result)
    log.info(
        "classify",
        extra=log_fields(priority=result.priority, n_tags=len(tags), n_similar=len(similar)),
    )
    return result


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
