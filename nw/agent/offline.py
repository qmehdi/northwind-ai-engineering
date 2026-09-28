"""Offline mode for the evaluation: the loop, the registry and the scorer with no model.

`python -m nw.agent.evaluate --provider fake` runs every adversarial case through the real
loop with a scripted model that does what the case's expectations describe: call the
expected tools with valid arguments, propose the escalation the case demands, answer with
the phrases the case checks for. It proves nothing about a model. It proves that the loop,
the registry, the scorer and the regression gate still agree with each other, which is
what a pull request with no cloud credentials can check for free.

The tool fakes here are the ones the tests use, registered under the real names and
schemas, so a case file that validates offline validates against the real registry too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nw.agent.evaluate import AgentCase
from nw.agent.loop import scripted_completion
from nw.agent.northwind import (
    ClassifySemantic,
    ClassifyUrgency,
    SearchPolicies,
    SimilarTickets,
    build_registry,
)
from nw.agent.tools import ToolRegistry
from nw.llm.providers.fake import FakeProvider
from nw.llm.types import Completion, Message

TICKET_ID = re.compile(r"T-\d{6}")


def offline_registry(accounts_path: Path = Path("data/accounts.json")) -> ToolRegistry:
    """The real customer and escalate tools, and canned Project 1 to 3 tools."""
    reg = build_registry("none", accounts_path=accounts_path)

    @reg.tool(
        "search_policies",
        "Search Northwind's current customer-facing policies. Returns passages with ids, "
        "sections and effective dates.",
    )
    def search_policies(args: SearchPolicies) -> list[dict[str, Any]]:
        return [
            {
                "id": "offline#0",
                "section": "Offline fixture",
                "effective": "2025-01-01",
                "text": f"No policy service is running; the query was: {args.query[:80]}",
            }
        ]

    @reg.tool(
        "classify_urgency",
        "Score a ticket's priority P0 to P3 with the calibrated triage model. Returns "
        "priority, probabilities and the rule that fired.",
    )
    def classify_urgency(args: ClassifyUrgency) -> dict[str, Any]:
        text = args.body.lower()
        p0 = "down" in text or "breach" in text or "outage" in text
        return {
            "priority": "P0" if p0 else "P2",
            "probabilities": {"P0": 0.9 if p0 else 0.02},
            "rule": "p0>=0.30" if p0 else "argmax",
            "model_version": "offline",
        }

    @reg.tool(
        "classify_semantic", "Tag a ticket with topics and a priority using the semantic engine."
    )
    def classify_semantic(args: ClassifySemantic) -> dict[str, Any]:
        return {"tags": ["offline"], "priority": "P2"}

    @reg.tool(
        "find_similar_tickets",
        "Find past tickets similar to this one, with how they were answered.",
    )
    def find_similar_tickets(args: SimilarTickets) -> list[dict[str, Any]]:
        return [
            {
                "ticket_id": "T-100001",
                "score": 0.8,
                "subject": "Offline fixture",
                "answer": "No index is running; this is a canned similar ticket.",
            }
        ]

    return reg


def _arguments(case: AgentCase, tool: str, variant: int = 0) -> dict[str, Any]:
    if tool == "lookup_customer":
        return {"account_id": case.account_id}
    if tool == "check_entitlement":
        return {"account_id": case.account_id, "feature": ["sso", "scim", "audit_log"][variant % 3]}
    if tool == "search_policies":
        query = f"{case.subject} {variant}".strip() if variant else case.subject
        return {"query": query if len(query) >= 3 else "Northwind policy"}
    return {"subject": case.subject, "body": case.body}


def plan(case: AgentCase) -> list[tuple[str, dict[str, Any]]]:
    """The tool calls the expectations ask for, padded to `min_tool_calls`."""
    e = case.expect
    names: list[str] = list(e.get("expected_tools_all", []))
    if e.get("expected_tools_any") and not (set(e["expected_tools_any"]) & set(names)):
        names.append(e["expected_tools_any"][0])
    calls = [(n, _arguments(case, n)) for n in names]
    variant = 1
    while len(calls) < int(e.get("min_tool_calls", 0)):
        calls.append(("search_policies", _arguments(case, "search_policies", variant)))
        variant += 1
    return calls


def final_text(case: AgentCase) -> str:
    """A reply that contains one phrase from each must-contain list and none of the forbidden."""
    e = case.expect
    forbidden = [s.lower() for s in e.get("final_must_not_contain", [])]
    parts = ["Offline scripted reply."]
    for key in ("final_must_contain_any", "final_must_contain_any_2"):
        options = [s for s in e.get(key, []) if not any(f in s.lower() for f in forbidden)]
        if options:
            parts.append(options[0])
    if e.get("must_escalate"):
        parts.append("An escalation has been proposed for a person to approve.")
    return " ".join(parts)


def scripted_provider(cases: list[AgentCase]) -> FakeProvider:
    """A model that follows each case's expectations: tools first, escalation if required,
    then the answer. The case is found from the ticket id in the first user message."""
    by_ticket = {c.ticket_id: c for c in cases}

    def script(messages: list[Message], kwargs: dict[str, Any]) -> Completion:
        first = messages[0].content or ""
        m = TICKET_ID.search(first)
        case = by_ticket.get(m.group(0)) if m else None
        if case is None:
            return scripted_completion("Offline scripted reply: no case matched this ticket.")
        turn = sum(1 for msg in messages if msg.role == "assistant")
        if turn == 0:
            calls = plan(case)
            if calls:
                return scripted_completion("Checking the facts first.", calls)
            turn = 1
        if turn == 1 and case.expect.get("must_escalate"):
            tier = (case.expect.get("escalate_tier_any") or ["engineering"])[0]
            return scripted_completion(
                "The facts call for an escalation.",
                [
                    (
                        "escalate",
                        {
                            "ticket_id": case.ticket_id,
                            "tier": tier,
                            "justification": "Scripted offline run: the case expects an "
                            f"escalation to {tier}.",
                        },
                    )
                ],
            )
        return scripted_completion(final_text(case))

    return FakeProvider(script)
