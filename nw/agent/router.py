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
from dataclasses import dataclass, replace
from typing import Any

from nw.agent.loop import run_agent
from nw.agent.screen import Screener
from nw.agent.tools import ToolRegistry
from nw.agent.trace import Trajectory
from nw.llm import LLMClient


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
    return await run_agent(task, registry, client, **run_kw)  # Route cheap first


def _read_triage(content: str) -> tuple[str, float]:
    import json

    try:
        d: dict[str, Any] = json.loads(content)
        return str(d.get("priority", "P2")), float(d.get("probabilities", {}).get("P0", 0.0))
    except (ValueError, AttributeError):
        return "P2", 0.0
