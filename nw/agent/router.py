"""Cheap-model-first routing: the capstone's cost lever.

Not every ticket needs the Workhorse and a tool loop. Project 1 scores a
ticket in two milliseconds for nothing. The router uses it as a pre-filter:

- confident P0: propose escalation directly, no model call at all
- P3 questions: the Economy model, fewer steps, smaller budget
- everything else: the Workhorse loop from Project 4

Cost per resolved ticket is measured before and after, on the same tickets,
so the saving is a number and not a slide.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

from nw.agent.loop import run_agent
from nw.agent.screen import Screener
from nw.agent.tools import ToolRegistry
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory
from nw.agent.version import agent_version
from nw.config import ModelRole
from nw.llm import LLMClient
from nw.policy.redact import redact_for_agent


@dataclass
class RoutingPolicy:
    p0_confidence: float = 0.6
    economy_priorities: tuple[str, ...] = ("P3",)
    economy_max_steps: int = 4
    economy_budget_usd: float = 0.05
    workhorse_max_steps: int = 10
    workhorse_budget_usd: float = 0.40


async def route(
    ticket_id: str,
    account_id: str,
    subject: str,
    body: str,
    registry: ToolRegistry,
    client: LLMClient,
    *,
    policy: RoutingPolicy | None = None,
    screener: Screener | None = None,
    max_steps: int | None = None,
    budget_usd: float | None = None,
    max_total_tokens: int | None = None,
    requested_by: str | None = None,
) -> Trajectory:
    """`screener` runs in front of any branch that calls a model. The P0 branch makes no
    model call, so the ticket text never reaches one and is not screened there.

    The request's caps only ever lower the policy's: `max_steps` and `budget_usd` cap both
    loops, `max_total_tokens` is the run's token budget. The tools are bound to `account_id`
    for the whole route, and the P0 branch's proposal is saved like any other, so a person
    approves it with the same command (`nw.agent.approve`)."""
    if not TICKET_RE.match(ticket_id):
        raise ValueError(f"not a ticket id: {ticket_id!r}")
    policy = capped(policy or RoutingPolicy(), max_steps=max_steps, budget_usd=budget_usd)
    task = f"Ticket {ticket_id} from account {account_id}\nSubject: {subject}\n\n{body}"
    from nw.agent.northwind import bind_account

    with bind_account(account_id):
        return await _route(
            ticket_id,
            account_id,
            subject,
            body,
            task,
            registry,
            client,
            policy=policy,
            screener=screener,
            max_total_tokens=max_total_tokens,
            requested_by=requested_by,
        )


TICKET_RE = re.compile(r"^T-\d{6}$")


def capped(
    policy: RoutingPolicy, *, max_steps: int | None = None, budget_usd: float | None = None
) -> RoutingPolicy:
    """The policy with the request's caps applied; a request can lower a cap, never raise it."""
    changes: dict[str, Any] = {}
    if max_steps is not None:
        changes["economy_max_steps"] = min(policy.economy_max_steps, max_steps)
        changes["workhorse_max_steps"] = min(policy.workhorse_max_steps, max_steps)
    if budget_usd is not None:
        changes["economy_budget_usd"] = min(policy.economy_budget_usd, budget_usd)
        changes["workhorse_budget_usd"] = min(policy.workhorse_budget_usd, budget_usd)
    return replace(policy, **changes) if changes else policy


async def _route(
    ticket_id: str,
    account_id: str,
    subject: str,
    body: str,
    task: str,
    registry: ToolRegistry,
    client: LLMClient,
    *,
    policy: RoutingPolicy,
    screener: Screener | None,
    max_total_tokens: int | None,
    requested_by: str | None,
) -> Trajectory:
    run_kw: dict[str, Any] = {
        "screener": screener,
        "max_total_tokens": max_total_tokens,
        "account_id": account_id,
        "requested_by": requested_by,
    }
    # SOLUTION BEGIN
    triage = await registry.execute("classify_urgency", {"subject": subject, "body": body})
    priority, p0 = _read_triage(triage.content) if triage.ok else ("P2", 0.0)
    if priority == "P0" and p0 >= policy.p0_confidence:
        t = Trajectory(
            run_id=f"route-{ticket_id}-{uuid.uuid4().hex[:6]}",
            agent="router",
            task=redact_for_agent(task),
            account_id=account_id,
            requested_by=requested_by,
        )
        # No prompt and no model on this branch; the version still names the tools.
        t.agent_version = agent_version("router:p0", registry.specs(), {})
        t.steps = [
            Step(
                index=0,
                tool="classify_urgency",
                observation=triage.content,
                latency_ms=triage.latency_ms,
            )
        ]
        t.tools_called = ["classify_urgency"]
        t.proposed_actions = [
            ProposedAction(
                tool="escalate",
                arguments={
                    "ticket_id": ticket_id,
                    "tier": "duty_manager",
                    "justification": f"Triage scored P0 with probability {p0:.2f}; "
                    "routed straight to the duty manager.",
                },
                step=0,
            )
        ]
        t.final = "P0 by triage: escalation proposed to the duty manager without a model call."
        t.terminated = Termination.ANSWER
        t.cost_usd = 0.0
        return t
    if priority in policy.economy_priorities:
        return await run_agent(
            task,
            registry,
            client,
            role=ModelRole.ECONOMY,
            max_steps=policy.economy_max_steps,
            budget_usd=policy.economy_budget_usd,
            agent_name="resolver-economy",
            **run_kw,
        )
    return await run_agent(
        task,
        registry,
        client,
        role=ModelRole.WORKHORSE,
        max_steps=policy.workhorse_max_steps,
        budget_usd=policy.workhorse_budget_usd,
        agent_name="resolver",
        **run_kw,
    )
    # STUB: return await run_agent(task, registry, client, **run_kw)  # Route cheap first
    # SOLUTION END


def _read_triage(content: str) -> tuple[str, float]:
    import json

    try:
        d: dict[str, Any] = json.loads(content)
        return str(d.get("priority", "P2")), float(d.get("probabilities", {}).get("P0", 0.0))
    except (ValueError, AttributeError):
        return "P2", 0.0
