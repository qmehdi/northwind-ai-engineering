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
is the one the promotion gate chose, and so is the graph: `serving.json` beside the artifact
says int8 or fp32 (the gate serves fp32 when int8 falls below a served bar). Environment,
all optional: NW_QUANTIZED (1 the int8 graph, 0 fp32, overriding the gate's choice),
NW_SEMANTIC_SHADOW_ARTIFACT (a second artifact, the same shapes of path, scored on
every request with its int8 graph, agreement counted, never served), NW_SEMANTIC_CAPTURE (a
JSONL file of requests and predictions for backtests), NW_SEMANTIC_DRIFT_WINDOW,
NW_SEMANTIC_DRIFT_MIN, NW_SEMANTIC_DRIFT_EVERY.

On a platform (ADR 0008) the artifact comes from the registry: `NW_MODEL_URI` (`s3://`,
`gs://`, `file://` or MLflow `models:/`) is fetched into a temp directory at startup and
`NW_MODEL_VERSION` names the version served; `NW_SEMANTIC_ARTIFACT` stays the fallback.
`/version` reports both with the tenant and the environment, which every `drift_alert` line
carries too. The same app answers the Agent Platform's custom container contract
(`AIP_HEALTH_ROUTE`, `AIP_PREDICT_ROUTE`, `{"instances": [...]}` in, `{"predictions": [...]}`
out) for the live Vertex endpoint.
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

from nw.api import UNVERSIONED, install_version_headers, mount_versioned, version_fields
from nw.auth import install_api_key
from nw.config import settings
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.metrics_export import start_metrics_export
from nw.policy.redact import redact_fields
from nw.ratelimit import install_rate_limit
from nw.semantic.artifacts import resolve
from nw.semantic.data import TAGS
from nw.semantic.monitor import ALERT, SemanticDriftMonitor
from nw.serving.download import ModelSource, model_source
from nw.serving.identity import bind_identity, identity
from nw.serving.vertex import install_vertex_routes
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
SHADOW = Counter("nw_semantic_shadow_total", "Shadow model comparisons", ["agree"])


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
    shadow = None
    shadow_version: str | None = None
    ready = False
    monitor: SemanticDriftMonitor = SemanticDriftMonitor(None)
    drift_every: int = 50
    seen: int = 0
    capture: Path | None = None
    source: ModelSource | None = None  # where the served artifact came from


state = State()


def _encoder(artifact: Path, quantized: bool) -> tuple:
    """The graph the service runs from one resolved artifact: its tokenizer, the int8 graph
    when it exists, the metadata and the file name. The served slot and the shadow slot
    load the same way."""
    from transformers import AutoTokenizer

    from nw.semantic.export import OnnxEncoder

    meta = json.loads((artifact / "metadata.json").read_text())
    tok_dir = artifact / "tokenizer"
    tokenizer = AutoTokenizer.from_pretrained(str(tok_dir) if tok_dir.exists() else meta["base"])
    file = (
        "model.int8.onnx" if quantized and (artifact / "model.int8.onnx").exists() else "model.onnx"
    )
    return OnnxEncoder(artifact / file, tokenizer, meta["max_length"]), tokenizer, meta, file


def load_all(artifact: Path, index_dir: Path | None, quantized: bool) -> None:
    artifact = resolve(artifact, serve=True)
    state.onnx, state.tokenizer, meta, file = _encoder(artifact, quantized)
    state.thresholds = np.load(artifact / "tag_thresholds.npy")
    state.version, state.fmt = meta["version"], file
    state.metadata = {k: v for k, v in meta.items() if k not in ("metrics", "tags", "history")}
    if index_dir and (index_dir / "tickets.faiss").exists():
        from nw.semantic.embed import Embedder, TicketIndex

        state.index = TicketIndex.load(index_dir)
        state.embedder = Embedder(json.loads((index_dir / "metadata.json").read_text())["embedder"])
    state.onnx.run(["probe"])
    _configure_mlops(artifact, quantized)
    state.ready = True


def _configure_mlops(artifact: Path, quantized: bool) -> None:
    """Drift baseline from the artifact's data profile, an optional shadow artifact, and an
    optional capture file. All three are best effort: the service classifies without them."""
    profile_path = artifact / "data_profile.json"
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else None
    state.monitor = SemanticDriftMonitor(
        profile,
        window=int(os.environ.get("NW_SEMANTIC_DRIFT_WINDOW", "500")),
        min_window=int(os.environ.get("NW_SEMANTIC_DRIFT_MIN", "200")),
    )
    state.drift_every = int(os.environ.get("NW_SEMANTIC_DRIFT_EVERY", "50"))
    state.seen = 0
    if not state.monitor.enabled:
        log.info("drift monitoring off: no data_profile.json in the artifact")
    shadow = os.environ.get("NW_SEMANTIC_SHADOW_ARTIFACT")
    state.shadow, state.shadow_version = None, None
    if shadow:
        try:
            encoder, _, meta, file = _encoder(resolve(shadow, serve=True), quantized)
            encoder.run(["probe"])
            state.shadow, state.shadow_version = encoder, meta["version"]
            log.info("shadow model loaded", extra=log_fields(version=meta["version"], format=file))
        except Exception as exc:  # noqa: BLE001
            log.error(
                "shadow model failed to load", extra=log_fields(artifact=shadow, error=str(exc))
            )
    capture = os.environ.get("NW_SEMANTIC_CAPTURE")
    state.capture = Path(capture) if capture else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    bind_identity()
    start_metrics_export("semantic")
    # The registry's artifact when the platform injected NW_MODEL_URI, else the local path.
    state.source = model_source(
        fallback=Path(os.environ.get("NW_SEMANTIC_ARTIFACT", "artifacts/semantic"))
    )
    artifact = state.source.path
    index_dir = Path(os.environ["NW_INDEX"]) if os.environ.get("NW_INDEX") else None
    try:
        load_all(artifact, index_dir, quantized=_quantized(artifact))
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
# Starlette runs the last-added middleware first: the key check runs before the limiter
# (which buckets by key id) and the version headers land on their 401 and 429 too.
install_rate_limit(app)
install_api_key(app)
install_version_headers(app)
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
    source = state.source
    return {
        # The registry's version when the platform pinned one, else the artifact's own.
        "model_version": source.version if source and source.version else state.version,
        "artifact_version": state.version,
        "model_uri": source.uri if source else None,
        **identity(),
        "format": state.fmt,
        "metadata": state.metadata,
        "shadow_version": state.shadow_version,
        "config_hash": settings().config_hash(),
        **version_fields(),
    }


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
                    **identity(),
                ),
            )


def _quantized(artifact: Path) -> bool:
    """`NW_QUANTIZED` when set; else the graph the promotion gate chose (`serving.json`);
    else int8."""
    env = os.environ.get("NW_QUANTIZED")
    if env is not None and env != "":
        return env == "1"
    from nw.semantic.promote import served_quantized

    chosen = served_quantized(artifact)
    return True if chosen is None else chosen


def _shadow(text: str, priority: str) -> str | None:
    """The shadow artifact's priority for the same text: counted against the served one,
    logged when it differs, never returned to the caller."""
    if state.shadow is None:
        return None
    _, prio_logits, _ = state.shadow.run([text])
    other = PRIORITIES[int(prio_logits[0].argmax())]
    SHADOW.labels(agree=str(other == priority).lower()).inc()
    if other != priority:
        log.info(
            "shadow_disagreement",
            extra=log_fields(served=priority, shadow=other, shadow_version=state.shadow_version),
        )
    return other


def _capture(ticket: TicketIn, result: Classification, shadow: str | None) -> None:
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
        "shadow_priority": shadow,
    }
    record = redact_fields(record, "subject", "body")
    state.capture.parent.mkdir(parents=True, exist_ok=True)
    with state.capture.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def classify_ticket(ticket: TicketIn) -> Classification:
    """One ticket through the graph with every side effect: metrics, drift window, shadow,
    capture and the log line. Both the course route and the platform route call it."""
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
    shadow = _shadow(text, result.priority)
    if shadow is not None:
        state.monitor.observe_shadow(shadow == result.priority)
    _capture(ticket, result, shadow)
    log.info(
        "classify",
        extra=log_fields(priority=result.priority, n_tags=len(tags), n_similar=len(similar)),
    )
    return result


@app.post("/classify", response_model=Classification)
def classify(ticket: TicketIn) -> Classification:
    return classify_ticket(ticket)


def vertex_predict(instances: list[dict], parameters: dict) -> list[dict]:
    """The Agent Platform contract: every instance is a ticket, every prediction a result."""
    return [classify_ticket(TicketIn.model_validate(i)).model_dump() for i in instances]


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


VERTEX_ROUTES = install_vertex_routes(app, vertex_predict, ready=lambda: state.ready)
# /v1/... is the API; the bare paths are deprecated aliases for one release. The platform's
# routes are addressed by their configured path and stay out of the mirror.
mount_versioned(app, exclude=UNVERSIONED | VERTEX_ROUTES)
