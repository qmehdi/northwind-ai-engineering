"""Project 3 as a service: grounded answers with citations, or a refusal.

NW_POLICY_INDEX=artifacts/policy uv run uvicorn nw.policy.service:app --port 8003
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from nw.config import settings
from nw.llm import LLMClient
from nw.llm.providers import make_provider
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.policy.answer import Answer, answer
from nw.policy.retrieval import PolicyIndex

log = get_logger("nw.policy.service")
REQUESTS = Counter("nw_policy_requests_total", "Requests", ["outcome"])
LATENCY = Histogram("nw_policy_latency_seconds", "Latency", buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8))
SPEND = Gauge("nw_policy_spend_usd_total", "Model spend since start")
TOKENS = Counter("nw_policy_tokens_total", "Tokens", ["kind"])
READY = Gauge("nw_policy_ready", "1 when the index is loaded")


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    audience: str = Field(default="customer", pattern=r"^(customer|internal)$")
    k: int = Field(default=8, ge=1, le=20)


class State:
    index: PolicyIndex | None = None
    client: LLMClient | None = None
    min_score: float = 0.0
    ready = False


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    index_dir = Path(os.environ.get("NW_POLICY_INDEX", "artifacts/policy"))
    state.min_score = float(os.environ.get("NW_POLICY_MIN_SCORE", "0.0"))
    try:
        from nw.policy.retrieval import CrossEncoderReranker, real_embeddings

        reranker = (
            CrossEncoderReranker() if os.environ.get("NW_POLICY_RERANK", "1") == "1" else None
        )
        state.index = PolicyIndex.load(
            index_dir, real_embeddings(), index_dir / "chunks.jsonl", reranker=reranker
        )
        s = settings()
        state.client = LLMClient(make_provider(s), settings=s)
        state.ready = True
        READY.set(1)
        log.info(
            "index loaded",
            extra=log_fields(chunks=len(state.index.chunks), rerank=reranker is not None),
        )
    except Exception as exc:  # noqa: BLE001
        state.ready = False
        READY.set(0)
        log.error("index failed to load", extra=log_fields(index=str(index_dir), error=str(exc)))
    yield
    state.ready = False


app = FastAPI(title="Northwind policy service", version="1.0", lifespan=lifespan)


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
def readyz() -> dict[str, str | int]:
    if not state.ready or state.index is None:
        raise HTTPException(503, "index not loaded")
    return {"status": "ready", "chunks": len(state.index.chunks)}


@app.post("/ask", response_model=Answer)
async def ask(req: Ask) -> Answer:
    if not state.ready or state.index is None or state.client is None:
        REQUESTS.labels(outcome="not_ready").inc()
        raise HTTPException(503, "index not loaded")
    t0 = time.perf_counter()
    before_usage = state.client.usage
    retrieved = state.index.retrieve(req.question, k=req.k, audience=req.audience)
    result = await answer(state.client, req.question, retrieved, min_score=state.min_score)
    LATENCY.observe(time.perf_counter() - t0)
    REQUESTS.labels(outcome="refused" if result.refused else "answered").inc()
    after = state.client.usage
    TOKENS.labels(kind="input").inc(after.input_tokens - before_usage.input_tokens)
    TOKENS.labels(kind="output").inc(after.output_tokens - before_usage.output_tokens)
    SPEND.set(state.client.spend_usd)
    log.info(
        "ask",
        extra=log_fields(
            refused=result.refused,
            reason=result.reason,
            citations=len(result.citations),
            audience=req.audience,
        ),
    )
    return result


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
