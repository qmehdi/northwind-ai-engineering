"""Project 2 as a service: tags, priority and similar tickets from the ONNX model.

    NW_SEMANTIC_ARTIFACT=artifacts/semantic NW_INDEX=artifacts/index \
      uv run uvicorn nw.semantic.service:app --port 8002

No torch in the runtime path: the ONNX graph and the tokenizer are enough,
which is what makes the container small and the cold start short.
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

from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.semantic.data import TAGS
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
    ready = False


state = State()


def load_all(artifact: Path, index_dir: Path | None, quantized: bool) -> None:
    from transformers import AutoTokenizer

    from nw.semantic.export import OnnxEncoder

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
    if index_dir and (index_dir / "tickets.faiss").exists():
        from nw.semantic.embed import Embedder, TicketIndex

        state.index = TicketIndex.load(index_dir)
        state.embedder = Embedder(json.loads((index_dir / "metadata.json").read_text())["embedder"])
    state.onnx.run(["probe"])
    state.ready = True


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
            ),
        )
    except Exception as exc:  # noqa: BLE001
        state.ready = False
        LOADED.set(0)
        log.error("model failed to load", extra=log_fields(artifact=str(artifact), error=str(exc)))
    yield
    state.ready = False


app = FastAPI(title="Northwind semantic engine", version="1.0", lifespan=lifespan)


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
    log.info(
        "classify",
        extra=log_fields(priority=result.priority, n_tags=len(tags), n_similar=len(similar)),
    )
    return result


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
