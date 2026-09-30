"""One FastAPI app for any agent role: a specialist (`NW_AGENT_ROLE=triage|policy|resolution`)
or the orchestrator (`NW_AGENT_ROLE=orchestrator`, with the specialists' URLs).

    NW_AGENT_ROLE=resolver uv run uvicorn nw.agent.service:app --port 8010   # the resolver
    NW_AGENT_ROLE=triage uv run uvicorn nw.agent.service:app --port 8011
    NW_AGENT_ROLE=policy uv run uvicorn nw.agent.service:app --port 8012
    NW_AGENT_ROLE=resolution uv run uvicorn nw.agent.service:app --port 8013
    NW_AGENT_ROLE=orchestrator NW_TRIAGE_AGENT_URL=http://localhost:8011 \
        NW_POLICY_AGENT_URL=... NW_RESOLUTION_AGENT_URL=... \
        uv run uvicorn nw.agent.service:app --port 8010

Endpoints: POST /run, POST /route (the capstone router), GET /healthz, /readyz, /version
(the agent version and its parts), /drift (the behaviour window), /metrics.

Operator controls, all environment variables: NW_AGENT_DISABLED=1 refuses /run and /route
with 503 and leaves readiness alone (the platform keeps the instance, the operator stops
the spend); NW_AGENT_MAX_CONCURRENT_RUNS (default 4) bounds runs in flight, 429 beyond it;
NW_AGENT_CAPTURE appends one JSON line per run; NW_AGENT_BASELINE points the drift monitor
at a baseline other than this track's data/golden/baselines/agent-<track>.json;
NW_AGENT_DRIFT_WINDOW and NW_AGENT_DRIFT_MIN (default 200) size the window;
NW_AGENT_JUDGE_SAMPLE (default 0.05) is the share of answered runs the Judge scores in the
background for the `nw_agent_judge_score` quality signal; NW_AGENT_MAX_TOTAL_TOKENS caps the
tokens one run may spend (input plus output), stopping it with BUDGET. A request may lower
that cap, never raise it.

Who and whose: every run records `requested_by`, the caller's API key id, so the approval
tool can refuse self-approval, and binds its customer tools to the request's `account_id`
(`/route` requires one; `/run` without one binds none and the account tools refuse).
Traces go to the ops store (`nw.agent.opstore`): `NW_TRACE_DIR` on a laptop, the tenant's
prefix in the platform's object storage when `NW_OPS_STORE` is set, redacted either way.

On a platform (ADR 0008) the runtime injects NW_TENANT and NW_ENVIRONMENT (on every log line,
every run line and every drift_alert), NW_AGENT_REGISTRY (the registry entry this agent runs
under) and NW_MEMORY_ID (its memory store); `/version` reports them. The gateway key comes
from NW_GATEWAY_KEY_SECRET_ARN or NW_GATEWAY_KEY_SECRET_NAME when NW_GATEWAY_KEY is unset.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import Field

from nw.agent.loop import SYSTEM_RULES, run_agent
from nw.agent.monitor import ALERT, AgentMonitor, JudgeSampler, RunSummary
from nw.agent.northwind import ACCOUNT_PATTERN, bind_account
from nw.agent.opstore import LocalStore, OpsStore, store_for
from nw.agent.orchestrator import (
    ORCHESTRATOR_SYSTEM,
    SPECIALISTS,
    SpecialistRequest,
    SpecialistResponse,
    run_orchestrator,
    run_specialist,
    subset,
)
from nw.agent.tools import Observation, ToolRegistry
from nw.agent.trace import Termination, Trajectory
from nw.agent.version import describe
from nw.api import install_version_headers, mount_versioned, version_fields
from nw.auth import install_api_key
from nw.config import ModelRole, settings
from nw.llm import LLMClient
from nw.llm.providers import make_provider
from nw.logging import bind_correlation_id, configure_logging, get_logger, log_fields
from nw.metrics_export import start_metrics_export
from nw.policy.redact import redact
from nw.ratelimit import install_rate_limit
from nw.serving.gateway import gateway_fields, resolve_gateway_key
from nw.serving.identity import bind_identity, identity
from nw.telemetry import configure_tracing, instrument_app

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
# AgentOps: per tool, per termination, the operator controls and the drift window.
TOOL_CALLS = Counter("nw_agent_tool_calls_total", "Tool calls by outcome", ["tool", "outcome"])
TOOL_LATENCY = Histogram(
    "nw_agent_tool_latency_seconds",
    "Tool latency",
    ["tool"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
TERMINATIONS = Counter("nw_agent_terminations_total", "Runs by termination", ["terminated"])
REJECTED = Counter(
    "nw_agent_rejected_total", "Runs refused before any model call", ["role", "reason"]
)
DISABLED = Gauge("nw_agent_disabled", "1 while the kill switch is on", ["role"])
IN_FLIGHT = Gauge("nw_agent_runs_in_flight", "Runs currently executing", ["role"])
VERSION_INFO = Gauge("nw_agent_version_info", "Agent version as a label", ["role", "version"])
DRIFT_CAP = Gauge("nw_agent_drift_cap_rate", "Share of recent runs stopped by a cap or an error")
DRIFT_ERR = Gauge("nw_agent_drift_error_rate", "Tool errors over tool calls in the window")
DRIFT_PSI = Gauge("nw_agent_drift_psi", "PSI against the baseline", ["feature"])
DRIFT_LEVEL = Gauge("nw_agent_drift_level", "0 ok, 1 watch, 2 alert, -1 warming up")


class State:
    screener = None
    role: str = "triage"
    registry: ToolRegistry | None = None
    client: LLMClient | None = None
    ready = False
    trace_dir = Path("artifacts/traces")
    traces: OpsStore | None = None  # None: a LocalStore on trace_dir, built on first use
    baseline: dict[str, Any] | None = None
    capture: Path | None = None
    max_runs: int = 4
    max_total_tokens: int | None = None
    settings: Any = None  # the settings the client was built with, gateway key resolved

    def __init__(self) -> None:
        # Mutable per instance: tests build a fresh State and must not share a window.
        self.urls: dict[str, str] = {}
        self.monitor: AgentMonitor = AgentMonitor(None)
        self.slots: asyncio.Semaphore | None = None
        self.sampler: JudgeSampler | None = None
        self.judge_tasks: set[asyncio.Task[None]] = set()


state = State()


def tool_metrics(obs: Observation) -> None:
    """The registry hook: one counter and one histogram per tool. Registered by the service;
    the registry has no Prometheus import."""
    outcome = "pending_approval" if obs.pending_approval else ("ok" if obs.ok else "error")
    TOOL_CALLS.labels(tool=obs.tool, outcome=outcome).inc()
    TOOL_LATENCY.labels(tool=obs.tool).observe(obs.latency_ms / 1000)


def attach_tool_metrics(registry: ToolRegistry) -> None:
    if tool_metrics not in registry.hooks:
        registry.hooks.append(tool_metrics)


def load_baseline(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def configure_agentops() -> None:
    """Drift baseline, capture file and the concurrency cap, from the environment. All best
    effort: the service runs without a baseline, it just cannot compute PSI."""
    from nw.agent.evaluate import baseline_path

    s = settings()
    default = baseline_path("live", s.track.value)  # this track's live baseline, if committed
    state.baseline = load_baseline(Path(os.environ.get("NW_AGENT_BASELINE", str(default))))
    state.monitor = AgentMonitor(
        state.baseline,
        window=int(os.environ.get("NW_AGENT_DRIFT_WINDOW", "500")),
        min_window=int(os.environ.get("NW_AGENT_DRIFT_MIN", "200")),
        workhorse=s.model_for(ModelRole.WORKHORSE),
    )
    if not state.monitor.enabled:
        log.info("drift PSI off", extra=log_fields(reason=state.monitor.psi_off))
    # The Judge scores a sample of live turns off the request path (NW_AGENT_JUDGE_SAMPLE,
    # a rate from 0 to 1, default one run in twenty; 0 turns it off).
    state.sampler = JudgeSampler(float(os.environ.get("NW_AGENT_JUDGE_SAMPLE", "0.05") or 0))
    capture = os.environ.get("NW_AGENT_CAPTURE")
    state.capture = Path(capture) if capture else None
    tokens = os.environ.get("NW_AGENT_MAX_TOTAL_TOKENS", "").strip()
    state.max_total_tokens = int(tokens) if tokens else None
    state.slots = None  # created lazily on the serving loop, see _slots


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("NW_LOG_FORMAT", "json"))
    bind_identity()
    start_metrics_export("agent")
    state.role = os.environ.get("NW_AGENT_ROLE", "triage")
    state.trace_dir = Path(os.environ.get("NW_TRACE_DIR", "artifacts/traces"))
    state.traces = store_for("trajectories", local=state.trace_dir)
    try:
        if state.role not in {*SPECIALISTS, "orchestrator", "resolver"}:
            raise ValueError(f"unknown NW_AGENT_ROLE {state.role!r}")
        s = resolve_gateway_key(settings())
        state.settings = s
        state.client = LLMClient(make_provider(s), settings=s)
        from nw.agent.screen import from_env

        state.screener = from_env()
        if state.role == "orchestrator":
            state.urls = {r: os.environ[f"NW_{r.upper()}_AGENT_URL"] for r in SPECIALISTS}
            from nw.agent.orchestrator import orchestrator_registry

            state.registry = orchestrator_registry(state.urls)  # for /version; runs build their own
        else:
            from nw.agent.northwind import build_registry

            state.registry = build_registry(os.environ.get("NW_TOOL_BACKEND", "local"))
        attach_tool_metrics(state.registry)
        configure_agentops()
        state.ready = True
        READY.labels(role=state.role).set(1)
        VERSION_INFO.labels(role=state.role, version=version_info()["agent_version"]).set(1)
        log.info(
            "agent ready",
            extra=log_fields(role=state.role, agent_version=version_info()["agent_version"]),
        )
    except Exception as exc:  # noqa: BLE001
        state.ready = False
        READY.labels(role=state.role).set(0)
        log.error("agent failed to start", extra=log_fields(role=state.role, error=str(exc)))
    yield
    state.ready = False
    http_client = getattr(state.registry, "http_client", None)
    if http_client is not None:
        await http_client.aclose()
    if state.client is not None:
        await state.client.aclose()


app = FastAPI(title="Northwind agent", version="1.0", lifespan=lifespan)
# Starlette runs the last-added middleware first: the key check runs before the limiter
# (which buckets by key id) and the version headers land on their 401 and 429 too.
install_rate_limit(app)
install_api_key(app)
install_version_headers(app)
configure_tracing("northwind-agent")
instrument_app(app)


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
    """Readiness is about the process, not the operator's decision: the kill switch does not
    fail it, so the platform keeps the instance while runs are refused."""
    if not state.ready:
        raise HTTPException(503, "agent not ready")
    return {"status": "ready", "role": state.role}


# ----- version -----------------------------------------------------------------------


def version_info() -> dict[str, Any]:
    """The agent version this role runs with, the same hash `run_agent` puts on a trajectory."""
    if state.registry is None or state.client is None:
        raise HTTPException(503, "agent not ready")
    if state.role == "orchestrator":
        system, specs = ORCHESTRATOR_SYSTEM, state.registry.specs()
    elif state.role in SPECIALISTS:
        spec = SPECIALISTS[state.role]
        system, specs = spec["system"], subset(state.registry, spec["tools"]).specs()
    else:
        system, specs = SYSTEM_RULES, state.registry.specs()
    models = {ModelRole.WORKHORSE.value: state.client.model_for(ModelRole.WORKHORSE)}
    return {"role": state.role, **describe(system, specs, models)}


@app.get("/version")
def version() -> dict[str, Any]:
    info = version_info()
    info["economy_model"] = state.client.model_for(ModelRole.ECONOMY) if state.client else None
    info["baseline"] = (
        {
            "agent_version": state.baseline.get("agent_version"),
            "written_at": state.baseline.get("written_at"),
        }
        if state.baseline
        else None
    )
    info["disabled"] = _disabled()
    info["max_concurrent_runs"] = _slots_limit()
    info["max_total_tokens"] = state.max_total_tokens
    s = settings()
    info["config_hash"] = s.config_hash()
    info["fallbacks"] = {r.value: m for r, m in s.fallback.items()}
    info["resilience"] = state.client.meter.resilience() if state.client else None
    # The platform's view of this agent (ADR 0008): whose it is, the registry entry it runs
    # under, its memory store and the gateway its model calls go through.
    info.update(identity())
    info["registry"] = os.environ.get("NW_AGENT_REGISTRY", "").strip() or None
    info["memory_id"] = os.environ.get("NW_MEMORY_ID", "").strip() or None
    info.update(gateway_fields(state.settings or s))
    info.update(version_fields())
    return info


# ----- operator controls -------------------------------------------------------------


def _disabled() -> bool:
    on = os.environ.get("NW_AGENT_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    DISABLED.labels(role=state.role).set(1 if on else 0)
    return on


def _slots_limit() -> int:
    return int(os.environ.get("NW_AGENT_MAX_CONCURRENT_RUNS", str(State.max_runs)))


def _slots() -> asyncio.Semaphore:
    if state.slots is None:
        state.max_runs = _slots_limit()
        state.slots = asyncio.Semaphore(state.max_runs)
    return state.slots


@asynccontextmanager
async def admit(endpoint: str) -> AsyncIterator[None]:
    """Readiness, the kill switch, then a slot. Refusals are metrics and log lines, never
    model calls."""
    if not state.ready or state.client is None:
        raise HTTPException(503, "agent not ready")
    if _disabled():
        REJECTED.labels(role=state.role, reason="disabled").inc()
        log.warning("run refused", extra=log_fields(endpoint=endpoint, reason="disabled"))
        raise HTTPException(
            503,
            "agent disabled by NW_AGENT_DISABLED: runs are refused, readiness is unchanged; "
            "unset it to resume",
        )
    sem = _slots()
    if sem.locked():
        REJECTED.labels(role=state.role, reason="concurrency").inc()
        raise HTTPException(
            429,
            f"{state.max_runs} runs already in flight (NW_AGENT_MAX_CONCURRENT_RUNS); retry",
        )
    async with sem:
        IN_FLIGHT.labels(role=state.role).inc()
        try:
            yield
        finally:
            IN_FLIGHT.labels(role=state.role).dec()


# ----- runs --------------------------------------------------------------------------


def token_cap(requested: int | None) -> int | None:
    """The run's token budget: the operator's cap, or the request's when it is lower. A
    request can never raise what `NW_AGENT_MAX_TOTAL_TOKENS` allows."""
    caps = [c for c in (requested, state.max_total_tokens) if c is not None]
    return min(caps) if caps else None


def caller(request: Request) -> str | None:
    """Who is asking: the API key id the key check resolved, `key:<id>`, or None."""
    key_id = getattr(request.state, "api_key_id", None)
    return f"key:{key_id}" if key_id else None


@app.post("/run", response_model=SpecialistResponse)
async def run(req: SpecialistRequest, request: Request) -> SpecialistResponse:
    async with admit("/run"):
        assert state.client is not None
        t0 = time.perf_counter()
        who = caller(request)
        req = req.model_copy(update={"max_total_tokens": token_cap(req.max_total_tokens)})
        if state.role == "orchestrator":
            with bind_account(req.account_id):
                t = await run_orchestrator(
                    req.task,
                    state.urls,
                    state.client,
                    max_steps=req.max_steps,
                    budget_usd=req.budget_usd,
                    hooks=[tool_metrics],
                    account_id=req.account_id,
                    requested_by=who,
                )
        elif state.role in SPECIALISTS:
            assert state.registry is not None
            _, t = await run_specialist(
                state.role,
                req,
                state.registry,
                state.client,
                screener=state.screener,
                requested_by=who,
            )
        else:
            # resolver: the whole registry and the hand-built loop
            assert state.registry is not None
            with bind_account(req.account_id):
                t = await run_agent(
                    req.task,
                    state.registry,
                    state.client,
                    max_steps=req.max_steps,
                    budget_usd=req.budget_usd,
                    agent_name=state.role,
                    screener=state.screener,
                    max_total_tokens=req.max_total_tokens,
                    account_id=req.account_id,
                    requested_by=who,
                )
        finish_run(t, state.role, t0)
        return _response(t)


def _response(t: Trajectory) -> SpecialistResponse:
    return SpecialistResponse(
        run_id=t.run_id,
        final=t.final,
        terminated=t.terminated.value,
        steps=t.n_steps,
        cost_usd=t.cost_usd,
        proposed_actions=[p.model_dump() for p in t.proposed_actions],
    )


class RouteRequest(SpecialistRequest):
    ticket_id: str = Field(default="T-000000", pattern=r"^T-\d{6}$")
    account_id: str = Field(default="NW-00000", pattern=ACCOUNT_PATTERN)
    subject: str = ""


@app.post("/route", response_model=SpecialistResponse)
async def route_ticket(req: RouteRequest, request: Request) -> SpecialistResponse:
    """Capstone endpoint: Project 1 first, then the cheapest loop that will do. The caps the
    request sets (`max_steps`, `budget_usd`, `max_total_tokens`) lower the router's own;
    fields left out keep the router's policy."""
    if state.registry is None:
        raise HTTPException(503, "agent not ready")
    async with admit("/route"):
        assert state.client is not None
        from nw.agent.router import route

        given = req.model_fields_set
        t0 = time.perf_counter()
        t = await route(
            req.ticket_id,
            req.account_id,
            req.subject,
            req.task,
            state.registry,
            state.client,
            screener=state.screener,
            max_steps=req.max_steps if "max_steps" in given else None,
            budget_usd=req.budget_usd if "budget_usd" in given else None,
            max_total_tokens=token_cap(req.max_total_tokens),
            requested_by=caller(request),
        )
        finish_run(t, t.agent, t0)
        return _response(t)


def trace_store() -> OpsStore:
    if state.traces is None:
        state.traces = LocalStore(state.trace_dir)
    return state.traces


def finish_run(t: Trajectory, role: str, t0: float) -> None:
    """Everything a finished run leaves behind besides its response: the trace (redacted, in
    the ops store), the run metrics, the drift window, and the capture line."""
    try:
        t.save(trace_store())
    except Exception as exc:  # noqa: BLE001
        # The customer's answer is not held hostage to the trace store; the alarm is the log.
        log.error("trace not saved", extra=log_fields(run_id=t.run_id, error=str(exc)))
    RUNS.labels(role=role, terminated=t.terminated.value).inc()
    TERMINATIONS.labels(terminated=t.terminated.value).inc()
    STEPS.labels(role=role).observe(t.n_steps)
    LATENCY.labels(role=role).observe(time.perf_counter() - t0)
    COST.labels(role=role).inc(t.cost_usd)
    for p in t.proposed_actions:
        PROPOSALS.labels(role=role, tool=p.tool).inc()
    summary = state.monitor.observe(t)
    _observe_drift()
    _capture(summary)
    _sample_judge(t)
    # One line per run on every path (the loop logs its own only when a model ran), with the
    # tenant and the environment so a platform can attribute runs and cost.
    log.info(
        "run_finished",
        extra=log_fields(
            run_id=t.run_id,
            role=role,
            agent=t.agent,
            agent_version=t.agent_version,
            terminated=t.terminated.value,
            steps=t.n_steps,
            cost_usd=round(t.cost_usd, 5),
            **identity(),
        ),
    )


def _sample_judge(t: Trajectory) -> None:
    """Score a sample of answered runs with the Judge in a background task: the customer's
    response never waits for it, and the score feeds `nw_agent_judge_score`."""
    if state.sampler is None or state.client is None or t.terminated is not Termination.ANSWER:
        return
    if not t.final or not state.sampler.should_sample():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop in this call path: skip the sample
    task = loop.create_task(_judge_run(t))
    state.judge_tasks.add(task)
    task.add_done_callback(state.judge_tasks.discard)


async def _judge_run(t: Trajectory) -> None:
    from nw.agent.evaluate import live_turn_verdict
    from nw.config import ProviderMode

    offline = (state.settings or settings()).provider is ProviderMode.FAKE
    try:
        assert state.client is not None
        verdict = await live_turn_verdict(state.client, t, offline=offline)
    except Exception as exc:  # noqa: BLE001
        log.warning("turn not judged", extra=log_fields(run_id=t.run_id, error=str(exc)))
        return
    state.monitor.observe_judge(verdict.score)
    state.monitor.snapshot()  # sets nw_agent_judge_score and logs quality_alert past the bar
    log.info(
        "turn_judged",
        extra=log_fields(run_id=t.run_id, score=verdict.score, offline=offline, **identity()),
    )


def _observe_drift() -> None:
    snap = state.monitor.snapshot()
    DRIFT_CAP.set(snap.cap_rate)
    DRIFT_ERR.set(snap.error_rate)
    DRIFT_LEVEL.set({"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}[snap.level])
    if snap.steps_psi is not None:
        DRIFT_PSI.labels(feature="steps").set(snap.steps_psi)
        DRIFT_PSI.labels(feature="cost").set(snap.cost_psi or 0.0)
    if snap.level == "alert":
        # The deployment alarms on this message for every service; same line, same alarm.
        log.warning(
            "drift_alert",
            extra=log_fields(
                role=state.role,
                cap_rate=snap.cap_rate,
                error_rate=snap.error_rate,
                steps_psi=snap.steps_psi,
                cost_psi=snap.cost_psi,
                window=snap.window,
                threshold=ALERT,
                reasons=snap.reasons,
                **identity(),
            ),
        )


def _capture(summary: RunSummary) -> None:
    if state.capture is None:
        return
    state.capture.parent.mkdir(parents=True, exist_ok=True)
    # The summary holds counts and ids, no free text; the line still passes through
    # redaction so a field added later cannot put an email on disk.
    with state.capture.open("a", encoding="utf-8") as f:
        f.write(redact(summary.model_dump_json()).text + "\n")


@app.get("/drift")
def drift() -> dict[str, Any]:
    """The behaviour window: termination mix, tool error rate, steps and cost against the
    baseline."""
    return state.monitor.snapshot().as_dict()


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


mount_versioned(app)  # /v1/... is the API; the bare paths are deprecated aliases for one release
