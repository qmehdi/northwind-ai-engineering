"""One FastAPI app for any agent role: a specialist (`NW_AGENT_ROLE=triage|policy|resolution`)
or the orchestrator (`NW_AGENT_ROLE=orchestrator`, with the specialists' URLs).

    NW_AGENT_ROLE=triage uv run uvicorn nw.agent.service:app --port 8011
    NW_AGENT_ROLE=policy uv run uvicorn nw.agent.service:app --port 8012
    NW_AGENT_ROLE=resolution uv run uvicorn nw.agent.service:app --port 8013
    NW_AGENT_ROLE=orchestrator NW_TRIAGE_AGENT_URL=http://localhost:8011 \
        NW_POLICY_AGENT_URL=... NW_RESOLUTION_AGENT_URL=... \
        uv run uvicorn nw.agent.service:app --port 8010
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from nw.agent.orchestrator import (
    SPECIALISTS,
    SpecialistRequest,
    SpecialistResponse,
    run_orchestrator,
    run_specialist,
)
from nw.agent.tools import ToolRegistry
from nw.config import settings
from nw.llm import LLMClient
from nw.llm.providers import make_provider
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields

log = get_logger("nw.agent.service")
RUNS = Counter("nw_agent_runs_total", "Agent runs", ["role", "terminated"])
STEPS = Histogram(
    "nw_agent_steps", "Steps per run", ["role"], buckets=(1, 2, 3, 4, 6, 8, 10, 15, 20)
)
LATENCY = Histogram(
    "nw_agent_latency_seconds", "Run latency", ["role"], buckets=(1, 2, 5, 10, 20, 40, 80)
)
COST = Counter("nw_agent_cost_usd_total", "Model spend", ["role"])
PROPOSALS = Counter(
    "nw_agent_proposed_actions_total", "Irreversible actions proposed", ["role", "tool"]
)
READY = Gauge("nw_agent_ready", "1 when ready", ["role"])


class State:
    role: str = "triage"
    registry: ToolRegistry | None = None
    client: LLMClient | None = None
    urls: dict[str, str] = {}
    ready = False
    trace_dir = Path("artifacts/traces")


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    state.role = os.environ.get("NW_AGENT_ROLE", "triage")
    state.trace_dir = Path(os.environ.get("NW_TRACE_DIR", "artifacts/traces"))
    try:
        s = settings()
        state.client = LLMClient(make_provider(s), settings=s)
        if state.role == "orchestrator":
            state.urls = {r: os.environ[f"NW_{r.upper()}_AGENT_URL"] for r in SPECIALISTS}
        else:
            from nw.agent.northwind import build_registry

            state.registry = build_registry(os.environ.get("NW_TOOL_BACKEND", "local"))
        state.ready = True
        READY.labels(role=state.role).set(1)
        log.info("agent ready", extra=log_fields(role=state.role))
    except Exception as exc:  # noqa: BLE001
        state.ready = False
        READY.labels(role=state.role).set(0)
        log.error("agent failed to start", extra=log_fields(role=state.role, error=str(exc)))
    yield
    state.ready = False


app = FastAPI(title="Northwind agent", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def correlation(request: Request, call_next):
    with bind_correlation_id(request.headers.get("x-correlation-id")) as cid:
        response = await call_next(request)
        response.headers["x-correlation-id"] = cid
        return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "role": state.role}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    if not state.ready:
        raise HTTPException(503, "agent not ready")
    return {"status": "ready", "role": state.role}


@app.post("/run", response_model=SpecialistResponse)
async def run(req: SpecialistRequest) -> SpecialistResponse:
    if not state.ready or state.client is None:
        raise HTTPException(503, "agent not ready")
    t0 = time.perf_counter()
    if state.role == "orchestrator":
        t = await run_orchestrator(
            req.task, state.urls, state.client, max_steps=req.max_steps, budget_usd=req.budget_usd
        )
        resp = SpecialistResponse(
            run_id=t.run_id,
            final=t.final,
            terminated=t.terminated.value,
            steps=t.n_steps,
            cost_usd=t.cost_usd,
            proposed_actions=[p.model_dump() for p in t.proposed_actions],
        )
    else:
        assert state.registry is not None
        resp, t = await run_specialist(state.role, req, state.registry, state.client)
    t.save(state.trace_dir)
    RUNS.labels(role=state.role, terminated=resp.terminated).inc()
    STEPS.labels(role=state.role).observe(resp.steps)
    LATENCY.labels(role=state.role).observe(time.perf_counter() - t0)
    COST.labels(role=state.role).inc(resp.cost_usd)
    for p in resp.proposed_actions:
        PROPOSALS.labels(role=state.role, tool=p["tool"]).inc()
    return resp


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
