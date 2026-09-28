"""Project 3 as a service: grounded answers with citations, or a refusal.

    NW_POLICY_INDEX=artifacts/policy uv run uvicorn nw.policy.service:app --port 8003

Endpoints:
- POST /ask          answer one question; carries answer_id, prompt_version, cached
- POST /feedback     {answer_id, verdict: helpful|wrong|unsafe, note}; appended to a JSONL file
- GET  /healthz      process is up
- GET  /readyz       index loaded (readiness: routable)
- GET  /version      the index manifest, staleness, prompt versions, model ids, cache stats
- GET  /drift        top-hit confidence PSI, refusal rate and answer length against the baseline
- GET  /metrics      Prometheus text format

Environment, all optional: NW_POLICY_INDEX, NW_POLICY_CORPUS (compared with the manifest at
startup; nw_policy_index_stale and one `index_stale` warning), NW_POLICY_MIN_SCORE,
NW_POLICY_RERANK, NW_POLICY_CACHE_TTL_S (0 is off) and NW_POLICY_CACHE_SIZE,
NW_POLICY_FEEDBACK (JSONL path), NW_POLICY_CAPTURE (JSONL of every /ask),
NW_POLICY_DRIFT_WINDOW, NW_POLICY_DRIFT_MIN, NW_POLICY_DRIFT_EVERY.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from nw.auth import install_api_key
from nw.config import ModelRole, settings
from nw.llm import LLMClient, prompts
from nw.llm.providers import make_provider
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.policy.answer import ANSWER_PROMPT, Answer, answer
from nw.policy.cache import ResponseCache
from nw.policy.feedback import DEFAULT_PATH as FEEDBACK_DEFAULT
from nw.policy.feedback import FeedbackIn, append_feedback
from nw.policy.manifest import is_stale
from nw.policy.monitor import ALERT, PolicyDriftMonitor
from nw.policy.retrieval import PolicyIndex
from nw.telemetry import configure_tracing, instrument_app

log = get_logger("nw.policy.service")
REQUESTS = Counter("nw_policy_requests_total", "Requests", ["outcome"])
LATENCY = Histogram("nw_policy_latency_seconds", "Latency", buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8))
SPEND = Gauge("nw_policy_spend_usd_total", "Model spend since start")
TOKENS = Counter("nw_policy_tokens_total", "Tokens", ["kind"])
READY = Gauge("nw_policy_ready", "1 when the index is loaded")
INDEX_STALE = Gauge("nw_policy_index_stale", "1 when the corpus no longer matches the index")
INDEX_INFO = Gauge("nw_policy_index_info", "Index corpus hash as a label", ["corpus_sha"])
CACHE = Counter("nw_policy_cache_total", "Response cache lookups", ["result"])
FEEDBACK = Counter("nw_policy_feedback_total", "Feedback verdicts", ["verdict"])
DRIFT = Gauge("nw_policy_drift_psi", "Population stability index against the baseline", ["feature"])
REFUSAL_RATE = Gauge("nw_policy_refusal_rate_window", "Refusal share over the drift window")
DRIFT_LEVEL = Gauge("nw_policy_drift_level", "0 ok, 1 watch, 2 alert, -1 warming up")

ANSWER_MEMORY = 5000  # recent answers kept for /feedback, by answer_id


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    audience: str = Field(default="customer", pattern=r"^(customer|internal)$")
    k: int = Field(default=8, ge=1, le=20)


class AskResponse(Answer):
    """An Answer plus what the service adds: an id to give feedback on, the model that
    answered, and whether it came from the cache."""

    answer_id: str
    model_id: str
    cached: bool = False


class State:
    index: PolicyIndex | None = None
    client: LLMClient | None = None
    min_score: float = 0.0
    ready = False
    stale: bool | None = None
    corpus: Path = Path("data/policies")
    cache: ResponseCache[tuple[Answer, float]] = ResponseCache()  # answer, top-hit confidence
    feedback_path: Path | None = FEEDBACK_DEFAULT
    capture: Path | None = None
    monitor: PolicyDriftMonitor = PolicyDriftMonitor(None)
    drift_every: int = 50
    seen: int = 0
    answers: OrderedDict[str, dict[str, Any]] = OrderedDict()


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
    _configure_llmops()
    yield
    state.ready = False


def _configure_llmops() -> None:
    """Staleness against the corpus, the response cache, the feedback and capture files, and
    the drift monitor from the manifest's baseline. All best effort: the service answers
    without any of them."""
    state.corpus = Path(os.environ.get("NW_POLICY_CORPUS", "data/policies"))
    manifest = state.index.manifest if state.index else None
    state.stale = is_stale(manifest, state.corpus) if state.index else None
    if state.index:
        INDEX_INFO.labels(corpus_sha=state.index.manifest_hash).set(1)
    INDEX_STALE.set(1 if state.stale else 0)
    if state.stale:
        log.warning(
            "index_stale",
            extra=log_fields(
                index_corpus_sha=manifest.get("corpus_sha256_12") if manifest else None,
                built_at=manifest.get("built_at") if manifest else None,
                corpus=str(state.corpus),
            ),
        )
    elif state.stale is None and state.index:
        log.info(
            "corpus not present, index staleness unknown",
            extra=log_fields(corpus=str(state.corpus)),
        )
    state.cache = ResponseCache.from_env()
    feedback = os.environ.get("NW_POLICY_FEEDBACK", str(FEEDBACK_DEFAULT))
    state.feedback_path = Path(feedback) if feedback else None
    capture = os.environ.get("NW_POLICY_CAPTURE")
    state.capture = Path(capture) if capture else None
    state.monitor = PolicyDriftMonitor(
        (manifest or {}).get("baseline"),
        window=int(os.environ.get("NW_POLICY_DRIFT_WINDOW", "500")),
        min_window=int(os.environ.get("NW_POLICY_DRIFT_MIN", "50")),
    )
    state.drift_every = int(os.environ.get("NW_POLICY_DRIFT_EVERY", "50"))
    state.seen = 0
    state.answers = OrderedDict()
    if not state.monitor.enabled:
        log.info("drift monitoring off: no baseline in the index manifest")


app = FastAPI(title="Northwind policy service", version="1.1", lifespan=lifespan)
install_api_key(app)
configure_tracing("northwind-policy")
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
def readyz() -> dict[str, str | int]:
    if not state.ready or state.index is None:
        raise HTTPException(503, "index not loaded")
    return {"status": "ready", "chunks": len(state.index.chunks)}


def _model_id() -> str:
    return state.client.model_for(ModelRole.WORKHORSE) if state.client else "none"


@app.get("/version")
def version() -> dict[str, Any]:
    """Everything that decides what an answer looks like: the index manifest and whether the
    corpus has moved on, the prompt versions now loaded against those at build, the model."""
    if state.index is None:
        raise HTTPException(503, "index not loaded")
    manifest = {k: v for k, v in state.index.manifest.items() if k != "baseline"}
    built = manifest.get("prompt_versions") or {}
    current = prompts.versions()
    return {
        "manifest": manifest,
        "index_stale": state.stale,
        "corpus": str(state.corpus),
        "prompt_versions": current,
        "prompts_changed_since_build": sorted(
            n for n, h in current.items() if n in built and built[n] != h
        ),
        "model_id": _model_id(),
        "min_score": state.min_score,
        "cache": state.cache.stats(),
        "drift_baseline": bool(state.monitor.enabled),
    }


@app.get("/drift")
def drift() -> dict[str, Any]:
    """PSI of the top-hit confidence and of answer length, and the refusal rate, against the
    baseline the index manifest carries."""
    return state.monitor.snapshot().as_dict()


def _observe(top_confidence: float, result: Answer) -> None:
    state.monitor.observe(top_confidence, result.refused, len(result.text))
    state.seen += 1
    if state.seen % state.drift_every == 0:
        snap = state.monitor.snapshot()
        levels = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}
        DRIFT_LEVEL.set(levels[snap.level])
        if snap.refusal_rate is not None:
            REFUSAL_RATE.set(snap.refusal_rate)
        if snap.confidence_psi is not None:
            DRIFT.labels(feature="confidence").set(snap.confidence_psi)
        if snap.answer_length_psi is not None:
            DRIFT.labels(feature="answer_length").set(snap.answer_length_psi)
        if snap.level == "alert":
            log.warning(
                "drift_alert",
                extra=log_fields(
                    confidence_psi=snap.confidence_psi,
                    refusal_rate=snap.refusal_rate,
                    baseline_refusal_rate=snap.baseline_refusal_rate,
                    answer_length_psi=snap.answer_length_psi,
                    window=snap.window,
                    threshold=ALERT,
                    index_corpus_sha=state.index.manifest_hash if state.index else None,
                    prompt_version=ANSWER_PROMPT.version,
                ),
            )


def _remember(resp: AskResponse, req: Ask, top_confidence: float) -> None:
    state.answers[resp.answer_id] = {
        "question": req.question,
        "audience": req.audience,
        "k": req.k,
        "text": resp.text,
        "citations": resp.citations,
        "confidence": resp.confidence,
        "top_confidence": top_confidence,
        "refused": resp.refused,
        "reason": resp.reason,
        "prompt_version": resp.prompt_version,
        "model_id": resp.model_id,
        "index_corpus_sha": state.index.manifest_hash if state.index else None,
        "cached": resp.cached,
    }
    while len(state.answers) > ANSWER_MEMORY:
        state.answers.popitem(last=False)


def _capture(resp: AskResponse, req: Ask, top_confidence: float, latency_ms: float) -> None:
    if state.capture is None:
        return
    record = {
        "ts": time.time(),
        "answer_id": resp.answer_id,
        "question": req.question,
        "audience": req.audience,
        "citations": resp.citations,
        "confidence": resp.confidence,
        "top_confidence": top_confidence,
        "refused": resp.refused,
        "reason": resp.reason,
        "prompt_version": resp.prompt_version,
        "cached": resp.cached,
        "model_id": resp.model_id,
        "latency_ms": round(latency_ms, 1),
    }
    state.capture.parent.mkdir(parents=True, exist_ok=True)
    with state.capture.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@app.post("/ask", response_model=AskResponse)
async def ask(req: Ask) -> AskResponse:
    if not state.ready or state.index is None or state.client is None:
        REQUESTS.labels(outcome="not_ready").inc()
        raise HTTPException(503, "index not loaded")
    t0 = time.perf_counter()
    model_id = _model_id()
    key = ResponseCache.key(
        req.question,
        req.audience,
        req.k,
        state.index.manifest_hash,
        ANSWER_PROMPT.version,
        model_id,
    )
    cached = state.cache.get(key) if state.cache.enabled else None
    if cached is not None:
        CACHE.labels(result="hit").inc()
        hit, top_confidence = cached
        resp = AskResponse(
            **hit.model_dump(), answer_id=uuid.uuid4().hex, model_id=model_id, cached=True
        )
        REQUESTS.labels(outcome="refused" if resp.refused else "answered").inc()
        _remember(resp, req, top_confidence)
        _capture(resp, req, top_confidence, (time.perf_counter() - t0) * 1000)
        log.info("ask", extra=log_fields(cached=True, refused=resp.refused, audience=req.audience))
        return resp
    if state.cache.enabled:
        CACHE.labels(result="miss").inc()
    before_usage = state.client.usage
    retrieved = state.index.retrieve(req.question, k=req.k, audience=req.audience)
    top_confidence = retrieved[0].confidence if retrieved else 0.0
    result = await answer(state.client, req.question, retrieved, min_score=state.min_score)
    latency_ms = (time.perf_counter() - t0) * 1000
    LATENCY.observe(latency_ms / 1000)
    REQUESTS.labels(outcome="refused" if result.refused else "answered").inc()
    after = state.client.usage
    TOKENS.labels(kind="input").inc(after.input_tokens - before_usage.input_tokens)
    TOKENS.labels(kind="output").inc(after.output_tokens - before_usage.output_tokens)
    SPEND.set(state.client.spend_usd)
    resp = AskResponse(**result.model_dump(), answer_id=uuid.uuid4().hex, model_id=model_id)
    state.cache.put(key, (result, top_confidence))
    _observe(top_confidence, result)
    _remember(resp, req, top_confidence)
    _capture(resp, req, top_confidence, latency_ms)
    log.info(
        "ask",
        extra=log_fields(
            refused=result.refused,
            reason=result.reason,
            citations=len(result.citations),
            audience=req.audience,
            top_confidence=round(top_confidence, 3),
            prompt_version=result.prompt_version,
            cached=False,
        ),
    )
    return resp


@app.post("/feedback")
def feedback(fb: FeedbackIn) -> dict[str, Any]:
    """Record a verdict on an answer. Wrong and unsafe verdicts are golden-set candidates:
    `python -m nw.policy.feedback --to-golden` prints them."""
    known = state.answers.get(fb.answer_id)
    if known is None:
        raise HTTPException(404, "unknown or expired answer_id")
    FEEDBACK.labels(verdict=fb.verdict).inc()
    record = {"answer_id": fb.answer_id, "verdict": fb.verdict, "note": fb.note, **known}
    if state.feedback_path is not None:
        append_feedback(state.feedback_path, record)
    log.info(
        "feedback",
        extra=log_fields(
            verdict=fb.verdict,
            answer_id=fb.answer_id,
            refused=known["refused"],
            prompt_version=known["prompt_version"],
        ),
    )
    return {
        "recorded": state.feedback_path is not None,
        "path": str(state.feedback_path) if state.feedback_path else None,
        "golden_candidate": fb.verdict != "helpful",
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
