"""The agent registry: one card per agent, what is deployed as opposed to what was intended.

The catalog (`nw.agent.catalog`, `data/use_cases.yaml`) says what an agent is for. The
registry says what it is: its version, its prompt, the tools it can see with their versions,
the models it calls, how it was evaluated, how it is reached, and who approved it. An agent
that is not in the registry is not in production.

This is the AWS Agent Registry of the AgentOps reference (blog "AgentOps: operationalize
agentic AI at scale with Amazon Bedrock AgentCore", 2026-06-01: "a centralized place to
discover, share, and reuse agents, MCP servers, tools, and agent skills", with a draft,
pending, approved workflow) and the discovery step of the Agentic AI Lens (2026-06-10),
AGENTOPS01-BP01 ("publish agent role definitions to AWS Agent Registry so that both human
operators and other agents can discover capabilities, understand scope, and route work")
and AGENTOPS01-BP02 ("catalog agent capabilities, availability status, and handoff
acceptance criteria"). Google's Agents Companion whitepaper (Kaggle, February 2025) reaches
the same shape from the other side: the agent card is what another agent reads before it
delegates.

    uv run python -m nw.agent.registry --list          # every card on disk
    uv run python -m nw.agent.registry --show router   # one card
    uv run python -m nw.agent.registry --write         # (re)write every card from code
    uv run python -m nw.agent.registry --check         # cards match code: tools, prompt, version
    uv run python -m nw.agent.registry --push router   # register the card with the track's runtime

A card is `data/agents/<name>.json`. `--write` builds it from code and the catalog, so the
version on the card is the version a trajectory carries and `/version` reports. `--check` is
what CI runs: it recomputes every card from code and fails on a tool, a prompt hash or a
version that moved without the card being rewritten. `--push` sends the card through the
platform's `AgentRuntime.register` (AgentCore's registry on AWS, Agent Engine on Google Cloud,
the compose stack's registry on the Local track); nothing is pushed without the flag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nw.agent import catalog as cat
from nw.agent.evaluate import BASELINE, TIERS, load_baseline
from nw.agent.version import describe, tool_fingerprint
from nw.config import ModelRole, Settings, Track, settings

CARDS = Path("data/agents")

# Which golden set stands behind which agent. The router runs the resolver's loop on the
# same tools, so the resolver's baseline covers it; the specialists and the orchestrator
# are covered by their contract tests until they have a golden set of their own.
EVALUATED_BY: dict[str, Path] = {"resolver": BASELINE, "router": BASELINE}


class ToolEntry(BaseModel):
    name: str
    version: str  # the hash of this one tool's spec: name, description, schema
    requires_approval: bool = False


class Evaluation(BaseModel):
    baseline: str | None = None
    agent_version: str | None = None
    written_at: str | None = None
    cases: int | None = None
    success: int | None = None
    cost_usd_per_resolution: float | None = None
    tiers: dict[str, dict[str, Any]] = Field(default_factory=dict)  # tier -> {bar, measured}
    note: str | None = None


class AgentCard(BaseModel):
    name: str
    kind: str
    track: str
    agent_version: str
    prompt: str  # name@hash, the way version.describe reports it
    prompt_hash: str
    tools_version: str
    tools: list[ToolEntry]
    models: dict[str, str]
    use_case: str
    owner: str
    business_outcome: str
    risk_class: str
    oversight: dict[str, str]
    data_classes: list[str]
    endpoints: list[str]
    evaluation: Evaluation
    approval: dict[str, str | None]
    runtime_id: str | None = None  # what AgentRuntime.register returned, once pushed
    written_at: str

    @property
    def tool_names(self) -> list[str]:
        return sorted(t.name for t in self.tools)


def _models_for(definition: cat.AgentDefinition, s: Settings) -> dict[str, str]:
    return {role: s.model_for(ModelRole(role)) for role in definition.roles}


def _evaluation(name: str) -> Evaluation:
    path = EVALUATED_BY.get(name)
    if path is None:
        return Evaluation(note="no golden set of its own; covered by the contract tests")
    b = load_baseline(path)
    if b is None:
        return Evaluation(baseline=str(path), note="baseline not on disk")
    agg = b.get("aggregate", {})
    measured = agg.get("tiers") or {}
    bars = b.get("tiers") or {}
    return Evaluation(
        baseline=str(path),
        agent_version=b.get("agent_version"),
        written_at=b.get("written_at"),
        cases=agg.get("n"),
        success=agg.get("success"),
        cost_usd_per_resolution=agg.get("cost_usd_per_resolution"),
        tiers={
            t.value: {"bar": bars.get(t.value, {}), "measured": measured.get(t.value)}
            for t in TIERS
        },
        note=None
        if measured
        else "the baseline predates the tiers; measured values arrive "
        "with the next --write-baseline",
    )


def build_card(
    definition: cat.AgentDefinition,
    use_case: cat.UseCase,
    s: Settings,
    *,
    models: dict[str, str] | None = None,
) -> AgentCard:
    """The card from code: the same `describe()` the service's `/version` uses, plus what the
    catalog says and what the baseline measured."""
    models = models or _models_for(definition, s)
    d = describe(definition.system, definition.specs, models)
    return AgentCard(
        name=definition.name,
        kind=definition.kind,
        track=s.track.value,
        agent_version=d["agent_version"],
        prompt=d["prompt"],
        prompt_hash=d["prompt"].split("@", 1)[1],
        tools_version=d["tools_version"],
        tools=[
            ToolEntry(
                name=spec.name,
                version=tool_fingerprint([spec]),
                requires_approval=spec.name in definition.requires_approval,
            )
            for spec in sorted(definition.specs, key=lambda x: x.name)
        ],
        models=models,
        use_case=use_case.id,
        owner=use_case.owner,
        business_outcome=use_case.business_outcome,
        risk_class=use_case.risk_class.value,
        oversight=dict(use_case.oversight),
        data_classes=list(use_case.data_classes),
        endpoints=list(definition.endpoints),
        evaluation=_evaluation(definition.name),
        approval={"state": use_case.approval.value, "approver": use_case.approver},
        written_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def card_path(name: str, cards: Path = CARDS) -> Path:
    return cards / f"{name}.json"


def write_card(card: AgentCard, cards: Path = CARDS) -> Path:
    cards.mkdir(parents=True, exist_ok=True)
    path = card_path(card.name, cards)
    path.write_text(card.model_dump_json(indent=1) + "\n")
    return path


def load_cards(cards: Path = CARDS) -> dict[str, AgentCard]:
    if not cards.exists():
        return {}
    return {
        p.stem: AgentCard.model_validate_json(p.read_text()) for p in sorted(cards.glob("*.json"))
    }


# ----- the check ----------------------------------------------------------------------


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def check(
    cards: dict[str, AgentCard],
    agents: dict[str, cat.AgentDefinition],
    catalog: cat.Catalog,
    s: Settings,
) -> CheckResult:
    """Every agent in code has a card, and the card is what the code is now. The version is
    recomputed with the card's own model ids, so the check is about code, not about which
    track the machine running it is on; a different track is a note."""
    r = CheckResult()
    for name, definition in sorted(agents.items()):
        card = cards.get(name)
        if card is None:
            r.problems.append(
                f"{name}: no card in {CARDS}; an agent not in the registry is not in production"
            )
            continue
        fresh = build_card(
            definition, catalog.get(name) or _placeholder(name), s, models=card.models
        )
        if card.tool_names != fresh.tool_names:
            r.problems.append(
                f"{name}: tools moved: card {card.tool_names}, code {fresh.tool_names}"
            )
        else:
            moved = [
                a.name
                for a, b in zip(card.tools, fresh.tools, strict=True)
                if a.version != b.version
            ]
            if moved:
                r.problems.append(f"{name}: tool specs changed since the card: {', '.join(moved)}")
        if card.prompt_hash != fresh.prompt_hash:
            r.problems.append(
                f"{name}: prompt hash {card.prompt_hash} on the card, {fresh.prompt_hash} in code"
            )
        if card.agent_version != fresh.agent_version:
            r.problems.append(
                f"{name}: agent_version {card.agent_version} on the card, "
                f"{fresh.agent_version} in code"
            )
        uc = catalog.get(name)
        if uc is None:
            r.problems.append(
                f"{name}: card names use case {card.use_case} but the catalog has none"
            )
        else:
            if card.use_case != uc.id:
                r.problems.append(f"{name}: card names use case {card.use_case}, catalog {uc.id}")
            if card.approval.get("state") != uc.approval.value:
                r.problems.append(
                    f"{name}: card says {card.approval.get('state')}, catalog says "
                    f"{uc.approval.value}"
                )
            if card.approval.get("state") != cat.Approval.APPROVED.value:
                r.problems.append(f"{name}: card is not approved")
        current = _models_for(definition, s)
        if current != card.models:
            r.notes.append(
                f"{name}: card written on the {card.track} track with {card.models}; this "
                f"machine resolves {current}"
            )
    for name in sorted(set(cards) - set(agents)):
        r.problems.append(f"{name}: a card with no agent in code; delete it or build the agent")
    return r


def _placeholder(name: str) -> cat.UseCase:
    return cat.UseCase(
        id=f"uc-{name}",
        agent=name,
        owner="product_owner",
        business_outcome="(no use case registered)",
        risk_class=cat.RiskClass.HIGH,
        allowed_tools=["none"],
        data_classes=["ticket_text"],
    )


# ----- push -------------------------------------------------------------------------


def push(card: AgentCard, s: Settings, cards: Path = CARDS) -> str:
    """Register the card with the track's agent runtime and record what it returned. The
    Local track registers too: the compose stack has a registry, so the flow is the same."""
    from nw.platform.base import platform_for, tenant_from_env

    platform = platform_for(s)
    runtime_id = platform.agents.register(tenant_from_env(s), card.model_dump(mode="json"))
    card.runtime_id = runtime_id
    write_card(card, cards)
    return runtime_id


# ----- output -------------------------------------------------------------------------


def format_list(cards: dict[str, AgentCard]) -> str:
    lines = [
        "| Agent | Version | Track | Tools | Models | Risk | Approval | Evaluated |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, c in sorted(cards.items()):
        ev = (
            f"{c.evaluation.success}/{c.evaluation.cases}"
            if c.evaluation.cases is not None
            else "contract tests"
        )
        lines.append(
            f"| {name} | {c.agent_version} | {c.track} | {len(c.tools)} "
            f"| {', '.join(c.models.values())} | {c.risk_class} "
            f"| {c.approval.get('state')} | {ev} |"
        )
    return "\n".join(lines)


def format_check(r: CheckResult) -> str:
    head = "AGENT REGISTRY OK" if r.passed else "AGENT REGISTRY FAILED"
    lines = [head]
    lines += [f"  FAIL {p}" for p in r.problems]
    lines += [f"  note {n}" for n in r.notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The agent registry: cards for what is deployed.")
    ap.add_argument("--cards", type=Path, default=CARDS)
    ap.add_argument("--catalog", type=Path, default=cat.CATALOG)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="every card on disk")
    g.add_argument("--show", metavar="NAME", help="one card as JSON")
    g.add_argument(
        "--write", nargs="*", metavar="NAME", help="write cards from code (all by default)"
    )
    g.add_argument("--check", action="store_true", help="cards match code; exit 1 otherwise")
    g.add_argument(
        "--push", nargs="*", metavar="NAME", help="register cards with the track's runtime"
    )
    args = ap.parse_args(argv)
    s = settings()
    if args.show:
        card = load_cards(args.cards).get(args.show)
        if card is None:
            print(f"no card for {args.show!r} in {args.cards}", file=sys.stderr)
            return 1
        print(card.model_dump_json(indent=1))
        return 0
    if args.write is not None or args.check or args.push is not None:
        catalog = cat.load(args.catalog)
        agents = cat.agents_in_code()
    if args.write is not None:
        names = args.write or sorted(agents)
        for name in names:
            uc = catalog.get(name)
            if name not in agents or uc is None:
                print(f"{name}: not an agent in code with a use case; skipped", file=sys.stderr)
                continue
            path = write_card(build_card(agents[name], uc, s), args.cards)
            print(f"{name}: {path}")
        return 0
    if args.check:
        r = check(load_cards(args.cards), agents, catalog, s)
        print(format_check(r))
        return 0 if r.passed else 1
    if args.push is not None:
        if s.track is Track.LOCAL and not getattr(s, "tenant", None):
            print(
                "no track configured (NW_TRACK is local and NW_TENANT unset): the cards stay on "
                "disk; set NW_TRACK, or NW_TENANT for the compose stack's registry",
                file=sys.stderr,
            )
            return 1
        cards = load_cards(args.cards)
        for name in args.push or sorted(cards):
            if name not in cards:
                print(f"{name}: no card; run --write first", file=sys.stderr)
                return 1
            try:
                runtime_id = push(cards[name], s, args.cards)
            except (ImportError, NotImplementedError) as exc:
                print(f"{name}: no agent runtime for track {s.track.value}: {exc}", file=sys.stderr)
                return 1
            print(f"{name}: registered as {runtime_id}")
        return 0
    print(format_list(load_cards(args.cards)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
