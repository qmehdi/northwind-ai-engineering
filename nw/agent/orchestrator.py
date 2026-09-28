"""The multi-agent shape: one orchestrator, specialist agents over HTTP.

The orchestrator is itself an agent whose tools are the specialists. Each
specialist runs the same loop with a narrower system prompt and a subset of
the registry, behind its own `/run` endpoint. That is the whole trick: an
agent-as-a-tool is just a tool whose implementation makes an HTTP call.

Why HTTP and not a function call: the specialists scale, deploy and fail
independently, and their traces carry their own run ids that the orchestrator
records, so a failed resolution can be replayed end to end.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from nw.agent.loop import SYSTEM_RULES, run_agent
from nw.agent.screen import Screener
from nw.agent.tools import ToolRegistry
from nw.agent.trace import Trajectory
from nw.llm import LLMClient

SPECIALISTS: dict[str, dict[str, Any]] = {
    "triage": {
        "tools": ["classify_urgency", "classify_semantic", "lookup_customer", "check_entitlement"],
        "system": SYSTEM_RULES
        + "\nYou are the triage specialist. Establish who the customer is, what plan they are on, "
        "and how urgent the ticket is. Reply with a short factual summary: account, tier, "
        "priority, tags, and any entitlement facts. Do not answer the customer.",
        "description": "Establish the customer, plan, urgency and tags for a ticket. "
        "Returns facts.",
    },
    "policy": {
        "tools": ["search_policies"],
        "system": SYSTEM_RULES
        + "\nYou are the policy specialist. Answer the question from Northwind's current policies "
        "with the passage ids you relied on, or say the policies do not cover it. Numbers exactly.",
        "description": "Answer a policy question with cited passages, or say it is not covered.",
    },
    "resolution": {
        "tools": ["find_similar_tickets", "escalate"],
        "system": SYSTEM_RULES
        + "\nYou are the resolution specialist. Given the triage facts and the policy answer, "
        "draft the reply to the customer and, only if the priority or the policy requires it, "
        "propose an escalation with a justification.",
        "description": "Draft the customer reply from the facts and the policy answer; "
        "propose escalation only when required.",
    },
}


class SpecialistRequest(BaseModel):
    task: str = Field(min_length=3)
    max_steps: int = Field(default=6, ge=1, le=20)
    budget_usd: float = Field(default=0.10, gt=0, le=5)


class SpecialistResponse(BaseModel):
    run_id: str
    final: str | None
    terminated: str
    steps: int
    cost_usd: float
    proposed_actions: list[dict[str, Any]]


def subset(registry: ToolRegistry, names: list[str]) -> ToolRegistry:
    sub = ToolRegistry(hooks=list(registry.hooks))  # the service's metrics follow the tools
    for n in names:
        if n in registry.tools:
            sub.register(registry.tools[n])
    return sub


async def run_specialist(
    role: str,
    req: SpecialistRequest,
    registry: ToolRegistry,
    client: LLMClient,
    *,
    screener: Screener | None = None,
) -> tuple[SpecialistResponse, Trajectory]:
    """The specialist's task arrives over HTTP from the orchestrator, so it is screened
    here, in front of the specialist's own loop, with the service's configured screener."""
    spec = SPECIALISTS[role]
    t = await run_agent(
        req.task,
        subset(registry, spec["tools"]),
        client,
        system=spec["system"],
        max_steps=req.max_steps,
        budget_usd=req.budget_usd,
        agent_name=role,
        screener=screener,
    )
    resp = SpecialistResponse(
        run_id=t.run_id,
        final=t.final,
        terminated=t.terminated.value,
        steps=t.n_steps,
        cost_usd=t.cost_usd,
        proposed_actions=[p.model_dump() for p in t.proposed_actions],
    )
    return resp, t


class AskSpecialist(BaseModel):
    task: str = Field(
        min_length=3,
        max_length=6000,
        description="What the specialist should do, with the facts it needs",
    )


def orchestrator_registry(
    urls: dict[str, str], *, http: httpx.AsyncClient | None = None
) -> ToolRegistry:
    """One tool per specialist. The tool's implementation is an HTTP call to `/run`."""
    reg = ToolRegistry()

    def make(role: str) -> None:
        raise NotImplementedError("Step 6: a specialist is a tool that makes an HTTP call")

    for role in urls:
        make(role)
    return reg


ORCHESTRATOR_SYSTEM = (
    SYSTEM_RULES
    + "\nYou are the orchestrator. You do not have direct tools; you have specialists. "
    "Ask the triage specialist first, then the policy specialist for anything the reply must "
    "quote, then the resolution specialist with the facts and the policy answer. Combine their "
    "outputs into the final reply to the support agent. Repeat any proposed escalation."
)


async def run_orchestrator(
    task: str,
    urls: dict[str, str],
    client: LLMClient,
    *,
    http: httpx.AsyncClient | None = None,
    max_steps: int = 8,
    budget_usd: float = 0.5,
    hooks: list[Any] | None = None,
) -> Trajectory:
    reg = orchestrator_registry(urls, http=http)
    reg.hooks = list(hooks or [])
    return await run_agent(
        task,
        reg,
        client,
        system=ORCHESTRATOR_SYSTEM,
        max_steps=max_steps,
        budget_usd=budget_usd,
        agent_name="orchestrator",
    )
