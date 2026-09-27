"""The resolver behind the two managed agent runtimes, one module for both contracts.

The Reference stack runs the same `nw-agent` image on the track's managed runtime, and
each runtime speaks to its container in its own shape:

- Amazon Bedrock AgentCore Runtime, protocol HTTP: `GET /ping` must answer
  `{"status": "Healthy"}` and `POST /invocations` takes the request body, on port 8080.
- Vertex AI Agent Engine: `POST /api/reasoning_engine` and
  `POST /api/stream_reasoning_engine` take `{"class_method": ..., "input": {...}}` and
  return `{"output": ...}`, on port 8000.

Both call the same code as the Session path's `/route`: Project 1 first, then the
cheapest loop that will do. The Session path app is mounted underneath, so `/route`,
`/run`, `/readyz`, `/healthz` and `/metrics` keep working and the runtime's probes and
the course's own health checks agree. The port comes from `PORT` through the image's
uvicorn command, so nothing here binds a socket.

    NW_AGENT_ROLE=resolver PORT=8080 uv run uvicorn nw.agent.agentcore:app --port 8080
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from nw.agent import service
from nw.agent.orchestrator import SpecialistResponse
from nw.agent.service import RouteRequest, route_ticket

CLASS_METHODS = ("route",)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Mounted apps get no lifespan of their own, so run the service's here: it loads
    the registry, the client and the screener into `service.state`."""
    async with service.app.router.lifespan_context(service.app):
        yield


app = FastAPI(title="Northwind resolver on a managed runtime", version="1.0", lifespan=lifespan)


def _request(body: dict[str, Any]) -> RouteRequest:
    task = body.get("task") or body.get("prompt")
    if not isinstance(task, str) or not task.strip():
        raise HTTPException(422, "body needs a non-empty 'task' or 'prompt'")
    fields = {k: body[k] for k in ("ticket_id", "account_id", "subject") if body.get(k)}
    return RouteRequest(task=task, **fields)


async def _route(body: dict[str, Any]) -> SpecialistResponse:
    return await route_ticket(_request(body))


# ----- Amazon Bedrock AgentCore Runtime, protocol HTTP --------------------------------


@app.get("/ping")
def ping() -> JSONResponse:
    if not service.state.ready:
        return JSONResponse({"status": "Unhealthy", "role": service.state.role}, status_code=503)
    return JSONResponse({"status": "Healthy", "role": service.state.role})


@app.post("/invocations", response_model=SpecialistResponse)
async def invocations(body: dict[str, Any]) -> SpecialistResponse:
    """`{"prompt": ...}` is the AgentCore convention; `task` plus `ticket_id`, `account_id`
    and `subject` is the course's `/route` body. Both are accepted."""
    return await _route(body)


# ----- Vertex AI Agent Engine ---------------------------------------------------------


def _agent_engine_input(body: dict[str, Any]) -> dict[str, Any]:
    method = body.get("class_method")
    if method not in CLASS_METHODS:
        raise HTTPException(
            400, f"unknown class_method {method!r}; expected one of {CLASS_METHODS}"
        )
    payload = body.get("input") or {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "'input' must be an object")
    return payload


@app.post("/api/reasoning_engine")
async def reasoning_engine(body: dict[str, Any]) -> dict[str, Any]:
    resp = await _route(_agent_engine_input(body))
    return {"output": resp.model_dump()}


@app.post("/api/stream_reasoning_engine")
async def stream_reasoning_engine(body: dict[str, Any]) -> StreamingResponse:
    """The router answers in one piece, so the stream is one newline-delimited JSON line.
    Routing runs before the response starts so a 503 or 422 still reaches the caller."""
    resp = await _route(_agent_engine_input(body))

    async def lines() -> AsyncIterator[bytes]:
        yield (json.dumps({"output": resp.model_dump()}) + "\n").encode()

    return StreamingResponse(lines(), media_type="application/x-ndjson")


# The Session path app underneath: /route, /run, /readyz, /healthz, /metrics, API key included.
app.mount("/", service.app)
