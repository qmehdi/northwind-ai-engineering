"""The use case catalog and the lifecycle roles: intent, registered before code.

An agent without a written purpose cannot be evaluated, only admired. The AWS AgentOps
reference (blog "AgentOps: operationalize agentic AI at scale with Amazon Bedrock AgentCore",
2026-06-01) starts the lifecycle with a centralized use case catalog where product owners
register use cases for assessment before anything is built, and the Agentic AI Lens
(2026-06-10) makes the same point as AGENTOPS01-BP01, "Establish well-defined agent roles,
responsibilities, and success criteria": every agent has a documented job description with
role, owned business outcome, autonomy boundary and measurable success criteria, and the
downstream controls (guardrails, thresholds, escalation) derive from it.

The catalog is `data/use_cases.yaml`: one entry per agent with an owner, a business outcome,
a risk class, the tools it may see, the data it touches and an approval state. Risk classes
carry the tiered oversight of AGENTSEC04-BP02, "Human-in-the-loop for critical decisions":
read-only operations proceed autonomously, low-risk writes need one reviewer, higher-risk
operations need stricter approval. The allowed tool list is the deterministic control of
AGENTSEC04-BP01 (schema validation, permission boundaries) and of AGENTREL02-BP01, "Design
agents for specific and atomic tasks": one prompt, a constrained tool set, so a compromised
agent cannot reach beyond its scope.

    uv run python -m nw.agent.catalog            # the table
    uv run python -m nw.agent.catalog --check    # every agent in code has an approved entry
    uv run python -m nw.agent.catalog --roles    # who owns which lifecycle step

`--check` reads the agents from code (`agents_in_code`): the resolver's registry, the three
specialists' subsets, the orchestrator's specialist tools and the capstone router. It fails
when an agent has no entry, its entry is not approved, or its tools exceed the allowed list.
The catalog is the source of intent; `nw.agent.registry` is the source of what is deployed.

The roles are the lifecycle personas the AgentOps blog names (product owner, domain expert,
platform engineer, developer, data engineer, QA engineer, plus the reviewer and the end user
the lens' human loops need), mapped to the step each owns in plan, develop, build, test and
release, deploy, maintain. The guide embeds `--roles` so a participant knows whose hat they
wear at each step. Google's Agents Companion whitepaper (Kaggle, February 2025) draws the
same line in its AgentOps section: the agent is the product, and the operations around it
are the job.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from nw.llm.types import ToolSpec

CATALOG = Path("data/use_cases.yaml")


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Approval(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"


# The tiered oversight of AGENTSEC04-BP02 (Agentic AI Lens, 2026-06-10), in course terms.
OVERSIGHT: dict[RiskClass, dict[str, str]] = {
    RiskClass.LOW: {
        "actions": "read-only tools",
        "human": "none in the loop; sampled trace review after the fact (make review)",
        "approvers": "one",
    },
    RiskClass.MEDIUM: {
        "actions": "may propose an irreversible action",
        "human": "one reviewer approves each proposal before it executes (make approve)",
        "approvers": "one",
    },
    RiskClass.HIGH: {
        "actions": "money moves or a message leaves the company",
        "human": "two reviewers or an out-of-band approval; no persistent trust grant",
        "approvers": "two",
    },
}

DATA_CLASSES: frozenset[str] = frozenset(
    {
        "ticket_text",
        "customer_account",
        "entitlements",
        "policy_corpus",
        "past_tickets",
        "escalation_queue",
    }
)

STAGES: tuple[str, ...] = ("plan", "develop", "build", "test_and_release", "deploy", "maintain")
STAGE_TITLES: dict[str, str] = {
    "plan": "Plan",
    "develop": "Develop",
    "build": "Build",
    "test_and_release": "Test and release",
    "deploy": "Deploy",
    "maintain": "Maintain",
}


@dataclass(frozen=True)
class Role:
    title: str
    owns: dict[str, str] = field(default_factory=dict)  # stage -> the step this role owns


# The lifecycle roles of the AgentOps blog (2026-06-01) and the step each owns in this course.
ROLES: dict[str, Role] = {
    "product_owner": Role(
        "Product owner",
        {
            "plan": "registers the use case: outcome, risk class, allowed tools, approver "
            "(data/use_cases.yaml)",
            "test_and_release": "reads the system tier of the gate report and signs the "
            "approval state",
            "maintain": "re-approves the use case when the outcome or the risk class changes",
        },
    ),
    "domain_expert": Role(
        "Domain expert",
        {
            "plan": "writes the escalation rules and the golden set expectations "
            "(data/adversarial/tickets.jsonl)",
            "test_and_release": "labels the calibration set the judge is scored against",
            "maintain": "labels sampled traces (make review) and turns them into candidate cases",
        },
    ),
    "platform_engineer": Role(
        "Platform engineer",
        {
            "build": "builds the images and issues the gateway key and budget per tenant",
            "deploy": "registers the card with the agent runtime (make agent-cards PUSH=1) and "
            "runs the canary",
            "maintain": "owns the drift alarms and the kill switch (NW_AGENT_DISABLED)",
        },
    ),
    "developer": Role(
        "Developer",
        {
            "develop": "the loop, the tools and the prompts; every change moves the agent version",
            "build": "writes the agent card (make agent-cards) and fixes what "
            "registry --check reports",
        },
    ),
    "data_engineer": Role(
        "Data engineer",
        {
            "develop": "the accounts fixture, the policy corpus and the capture file the "
            "backtest reads",
            "maintain": "runs the capture and backtest loop and refreshes the baseline",
        },
    ),
    "qa_engineer": Role(
        "QA engineer",
        {
            "develop": "tool-tier tests: every tool with valid and with invalid arguments",
            "test_and_release": "runs the tiered gate (make agent-gate-offline, make agent-gate) "
            "and keeps the tier bars in data/golden/agent_baseline.json",
        },
    ),
    "reviewer": Role(
        "Reviewer",
        {
            "deploy": "approves or rejects proposed escalations (make approve RUN=<run_id>)",
            "maintain": "reviews the approval log and guardrail interventions each month",
        },
    ),
    "end_user": Role(
        "End user",
        {
            "plan": "the support agent who names the outcome the use case must deliver",
            "maintain": "reads the reply and sends feedback (POST /feedback on the policy service)",
        },
    ),
}


class UseCase(BaseModel):
    id: str = Field(pattern=r"^uc-[a-z0-9-]+$")
    agent: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    owner: str
    business_outcome: str = Field(min_length=10)
    risk_class: RiskClass
    allowed_tools: list[str] = Field(min_length=1)
    data_classes: list[str] = Field(min_length=1)
    approval: Approval = Approval.DRAFT
    approver: str | None = None
    co_approver: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> UseCase:
        if self.owner not in ROLES:
            raise ValueError(f"{self.id}: owner {self.owner!r} is not a lifecycle role")
        unknown = sorted(set(self.data_classes) - DATA_CLASSES)
        if unknown:
            raise ValueError(f"{self.id}: unknown data classes {unknown}")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError(f"{self.id}: allowed_tools has duplicates")
        if self.approval is Approval.APPROVED:
            if not self.approver:
                raise ValueError(f"{self.id}: approved without an approver")
            if self.risk_class is RiskClass.HIGH and not self.co_approver:
                raise ValueError(
                    f"{self.id}: high risk needs a co_approver "
                    f"({OVERSIGHT[RiskClass.HIGH]['human']})"
                )
        return self

    @property
    def oversight(self) -> dict[str, str]:
        return OVERSIGHT[self.risk_class]


class Catalog(BaseModel):
    use_cases: list[UseCase]

    @model_validator(mode="after")
    def _unique(self) -> Catalog:
        ids = [u.id for u in self.use_cases]
        agents = [u.agent for u in self.use_cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate use case ids")
        if len(set(agents)) != len(agents):
            raise ValueError("two use cases name the same agent")
        return self

    def by_agent(self) -> dict[str, UseCase]:
        return {u.agent: u for u in self.use_cases}

    def get(self, agent: str) -> UseCase | None:
        return self.by_agent().get(agent)


def load(path: Path = CATALOG) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(path.read_text()) or {})


# ----- the agents as code defines them ----------------------------------------------


@dataclass
class AgentDefinition:
    """What the code says an agent is: its prompt, the tools it can see and the model roles
    it calls. The registry hashes these into the agent version; the catalog checks the
    tools against the allowed list."""

    name: str
    kind: str  # resolver, specialist, orchestrator, router
    system: str
    specs: list[ToolSpec]
    roles: tuple[str, ...]  # model roles the agent calls, in nw.config.ModelRole values
    endpoints: list[str]
    requires_approval: frozenset[str] = frozenset()

    @property
    def tools(self) -> list[str]:
        return sorted(s.name for s in self.specs)


def agents_in_code(accounts_path: Path = Path("data/accounts.json")) -> dict[str, AgentDefinition]:
    """Every agent the course ships, read from the modules that define them. Building the
    registries is cheap: the local tools load their models on first call, not at import."""
    import httpx

    from nw.agent.loop import SYSTEM_RULES
    from nw.agent.northwind import build_registry
    from nw.agent.orchestrator import (
        ORCHESTRATOR_SYSTEM,
        SPECIALISTS,
        orchestrator_registry,
        subset,
    )

    full = build_registry("local", accounts_path=accounts_path)
    approval = frozenset(n for n, t in full.tools.items() if t.requires_approval)
    service = ["POST /run", "GET /version", "GET /drift", "GET /metrics"]
    out: dict[str, AgentDefinition] = {
        "resolver": AgentDefinition(
            "resolver", "resolver", SYSTEM_RULES, full.specs(), ("workhorse",), service, approval
        )
    }
    for role, spec in SPECIALISTS.items():
        out[role] = AgentDefinition(
            role,
            "specialist",
            spec["system"],
            subset(full, spec["tools"]).specs(),
            ("workhorse",),
            service,
            approval,
        )
    urls = {role: f"http://{role}" for role in SPECIALISTS}
    orch = orchestrator_registry(urls, http=httpx.AsyncClient())
    out["orchestrator"] = AgentDefinition(
        "orchestrator", "orchestrator", ORCHESTRATOR_SYSTEM, orch.specs(), ("workhorse",), service
    )
    out["router"] = AgentDefinition(
        "router",
        "router",
        SYSTEM_RULES,
        full.specs(),
        ("workhorse", "economy"),
        [
            "POST /route",
            "POST /invocations (AgentCore)",
            "POST /api/reasoning_engine (Agent Engine)",
            *service,
        ],
        approval,
    )
    return out


# ----- the check ----------------------------------------------------------------------


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def check(catalog: Catalog, agents: dict[str, AgentDefinition]) -> CheckResult:
    """An agent in code needs an approved entry whose allowed tools cover what it can see.
    An entry with no agent in code is a use case registered before it is built: a note."""
    r = CheckResult()
    entries = catalog.by_agent()
    for name, a in sorted(agents.items()):
        uc = entries.get(name)
        if uc is None:
            r.problems.append(f"{name}: no use case in the catalog; register it before it runs")
            continue
        if uc.approval is not Approval.APPROVED:
            r.problems.append(f"{name}: use case {uc.id} is {uc.approval.value}, not approved")
        extra = sorted(set(a.tools) - set(uc.allowed_tools))
        if extra:
            r.problems.append(
                f"{name}: sees tools outside {uc.id}'s allowed list: {', '.join(extra)}"
            )
        stale = sorted(set(uc.allowed_tools) - set(a.tools))
        if stale:
            r.notes.append(
                f"{name}: {uc.id} allows tools the agent does not have: {', '.join(stale)}"
            )
        irreversible = sorted(set(a.tools) & a.requires_approval)
        if uc.risk_class is RiskClass.LOW and irreversible:
            r.problems.append(
                f"{name}: {uc.id} is low risk but sees an irreversible tool "
                f"({', '.join(irreversible)}); low risk means read-only tools"
            )
    for name, uc in sorted(entries.items()):
        if name not in agents:
            r.notes.append(
                f"{uc.id}: agent {name!r} is registered and not built ({uc.approval.value})"
            )
    return r


# ----- output -------------------------------------------------------------------------


def format_table(catalog: Catalog, agents: dict[str, AgentDefinition] | None = None) -> str:
    lines = [
        "| Use case | Agent | Owner | Risk | Oversight | Tools | Approval |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for u in catalog.use_cases:
        built = "" if agents is None else ("" if u.agent in agents else " (not built)")
        approval = u.approval.value + (f" by {u.approver}" if u.approver else "")
        lines.append(
            f"| {u.id} | {u.agent}{built} | {ROLES[u.owner].title} | {u.risk_class.value} "
            f"| {u.oversight['human']} | {', '.join(u.allowed_tools)} | {approval} |"
        )
    return "\n".join(lines)


def format_roles() -> str:
    lines = ["| Role | Stage | Owns |", "| --- | --- | --- |"]
    for stage in STAGES:
        for role in ROLES.values():
            if stage in role.owns:
                lines.append(f"| {role.title} | {STAGE_TITLES[stage]} | {role.owns[stage]} |")
    return "\n".join(lines)


def roles_json() -> dict[str, Any]:
    return {
        "stages": [{"id": s, "title": STAGE_TITLES[s]} for s in STAGES],
        "roles": {k: {"title": r.title, "owns": dict(r.owns)} for k, r in ROLES.items()},
    }


def format_check(r: CheckResult) -> str:
    head = "USE CASE CATALOG OK" if r.passed else "USE CASE CATALOG FAILED"
    lines = [head]
    lines += [f"  FAIL {p}" for p in r.problems]
    lines += [f"  note {n}" for n in r.notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The use case catalog and the lifecycle roles.")
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--check", action="store_true", help="fail unless every agent is covered")
    ap.add_argument("--roles", action="store_true", help="who owns which lifecycle step")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.roles:
        print(json.dumps(roles_json(), indent=1) if args.json else format_roles())
        return 0
    catalog = load(args.catalog)
    agents = agents_in_code()
    if args.check:
        result = check(catalog, agents)
        if args.json:
            print(json.dumps({"passed": result.passed, **result.__dict__}, indent=1))
        else:
            print(format_check(result))
        return 0 if result.passed else 1
    if args.json:
        print(
            json.dumps(
                {"use_cases": [u.model_dump(mode="json") for u in catalog.use_cases]}, indent=1
            )
        )
    else:
        print(format_table(catalog, agents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
