"""Fakes for the Northwind tools, registered under the real names and schemas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nw.agent.northwind import (
    ClassifyUrgency,
    SearchPolicies,
    SimilarTickets,
    build_registry,
)

ACCOUNTS = [
    {
        "account_id": "NW-10000",
        "company": "Blue Freight",
        "tier": "Enterprise",
        "region": "eu",
        "seats": 900,
        "industry": "logistics",
        "features": ["dashboards", "api_full", "sso", "scim", "audit_log", "priority_support"],
        "sla_hours": 4,
    },
    {
        "account_id": "NW-10007",
        "company": "Quill Labs",
        "tier": "Starter",
        "region": "us",
        "seats": 8,
        "industry": "media",
        "features": ["dashboards", "api_basic"],
        "sla_hours": 72,
    },
]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps(ACCOUNTS))
    import nw.agent.northwind as nwmod

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    reg = build_registry("none", accounts_path=accounts)

    @reg.tool("search_policies", "Search policies (fake).")
    def search_policies(args: SearchPolicies):
        if "uptime" in args.query.lower():
            return [
                {
                    "id": "sla-2025#abc",
                    "section": "Uptime",
                    "effective": "2025-03-01",
                    "text": "Enterprise 99.95 percent",
                }
            ]
        return []

    @reg.tool("classify_urgency", "Triage (fake).")
    def classify_urgency(args: ClassifyUrgency):
        p0 = "down" in args.body.lower() or "breach" in args.body.lower()
        return {
            "priority": "P0" if p0 else "P2",
            "probabilities": {"P0": 0.9 if p0 else 0.02},
            "rule": "p0>=0.30" if p0 else "argmax",
            "model_version": "fake",
        }

    @reg.tool("find_similar_tickets", "Similar (fake).")
    def find_similar_tickets(args: SimilarTickets):
        return [
            {
                "ticket_id": "T-100001",
                "score": 0.8,
                "subject": "Similar one",
                "answer": "We restarted the sync.",
            }
        ]

    return reg


@pytest.fixture
def escalation_file(tmp_path) -> Path:
    return tmp_path / "escalations.jsonl"


@pytest.fixture
def make_agent_app(registry, make_client, monkeypatch, tmp_path):
    """The agent service with an injected fake client and no lifespan, for any role.
    Extra keyword arguments are set on the service state (monitor, capture, ...)."""
    from contextlib import asynccontextmanager

    from nw.agent import service

    @asynccontextmanager
    async def noop(app):
        yield

    def _make(provider, role: str = "resolver", **state_kw):
        monkeypatch.setattr(service, "state", service.State())
        service.state.role = role
        service.state.registry = registry
        service.state.trace_dir = tmp_path / "traces"
        service.state.client = make_client(provider)
        service.state.ready = True
        service.attach_tool_metrics(registry)
        for k, v in state_kw.items():
            setattr(service.state, k, v)
        service.app.router.lifespan_context = noop
        return service.app

    return _make
