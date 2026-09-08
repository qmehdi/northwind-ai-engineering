"""Cheap-model-first routing: the capstone's cost lever.

Not every ticket needs the Workhorse and a tool loop. Project 1 scores a
ticket in two milliseconds for nothing. The router uses it as a pre-filter:

- confident P0: propose escalation directly, no model call at all
- P3 questions: the Economy model, fewer steps, smaller budget
- everything else: the Workhorse loop as in Session 5

Cost per resolved ticket is measured before and after, on the same tickets,
so the saving is a number and not a slide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nw.agent.loop import run_agent
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
    workhorse_budget_usd: float = 0.25


async def route(
    ticket_id: str,
    account_id: str,
    subject: str,
    body: str,
    registry: ToolRegistry,
    client: LLMClient,
    *,
    policy: RoutingPolicy | None = None,
) -> Trajectory:
    policy = policy or RoutingPolicy()
    task = f"Ticket {ticket_id} from account {account_id}\nSubject: {subject}\n\n{body}"
    return await run_agent(task, registry, client, agent_name="resolver")  # Step 5: route


def _read_triage(content: str) -> tuple[str, float]:
    import json

    try:
        d: dict[str, Any] = json.loads(content)
        return str(d.get("priority", "P2")), float(d.get("probabilities", {}).get("P0", 0.0))
    except (ValueError, AttributeError):
        return "P2", 0.0
