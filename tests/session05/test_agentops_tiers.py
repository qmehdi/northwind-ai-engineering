"""AgentOps: tiered evaluation. Every check and metric carries a tier (tool, turn, session,
system); the report groups by tier; the gate reads a bar per tier from the baseline and
names the tier in every reason. All of it runs with the scripted provider."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nw.agent.evaluate import (
    BASELINE,
    DEFAULT_TIER_BARS,
    JUDGE_PROMPT,
    AgentCase,
    CaseScore,
    GatePolicy,
    JudgeVerdict,
    Tier,
    TierScore,
    aggregate,
    baseline_from,
    evaluate,
    format_decision,
    format_report,
    gate,
    llm_judge,
    load_baseline,
    load_cases,
    score,
    scripted_judge,
    tier_of,
    tiered,
    tool_tier,
)
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory
from nw.llm import LLMClient
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

ROOT = Path(__file__).resolve().parents[2]
ADVERSARIAL = ROOT / "data" / "adversarial" / "tickets.jsonl"
CASE_FILES = [
    ROOT / "data" / "adversarial" / "tickets.jsonl",
    ROOT / "data" / "golden" / "agent_cases.jsonl",
]


def _case(expect: dict, cid: str = "c1") -> AgentCase:
    return AgentCase(
        id=cid,
        kind="x",
        account_id="NW-10000",
        ticket_id="T-200001",
        subject="s",
        body="b",
        expect=expect,
    )


def _traj(final, steps, proposed=None, terminated=Termination.ANSWER, cost=0.01):
    t = Trajectory(
        run_id="r1", agent="resolver", task="t", final=final, terminated=terminated, cost_usd=cost
    )
    t.steps = steps
    t.tools_called = [s.tool for s in steps if s.tool]
    t.proposed_actions = [
        ProposedAction(tool="escalate", arguments=a, step=0) for a in (proposed or [])
    ]
    return t


def test_every_deterministic_failure_has_a_tier():
    assert tier_of("final lacks any of ['x']") is Tier.TURN
    assert tier_of("missing tools {'a'}") is Tier.TURN
    assert tier_of("did not propose escalation") is Tier.SYSTEM
    assert tier_of("escalated to the wrong tier: ['billing']") is Tier.SYSTEM
    assert tier_of("terminated by max_steps") is Tier.SESSION
    assert tier_of("an escalation was executed without approval") is Tier.SESSION


def test_tool_tier_counts_invalid_arguments_and_errors_per_tool():
    steps = [
        Step(index=0, tool="search_policies", observation="ok", ok=True),
        Step(index=1, tool="search_policies", observation="search_policies failed: boom", ok=False),
        Step(
            index=2,
            tool="lookup_customer",
            observation="invalid arguments for lookup_customer: account_id: bad",
            ok=False,
        ),
        Step(index=3, tool="screen:heuristic", observation="allowed", ok=True),
    ]
    ts = tool_tier(_traj("done", steps))
    assert (
        not ts.passed and "lookup_customer called with invalid arguments at step 2" in ts.failures
    )
    assert ts.metrics["calls"] == 3 and ts.metrics["invalid_arguments"] == 1
    assert ts.metrics["per_tool"]["search_policies"] == {"calls": 2, "errors": 1, "invalid": 0}


def test_turn_session_and_system_tiers_on_one_case():
    case = _case({"must_not_escalate": True, "final_must_contain_any": ["30 days"]})
    good = _traj("Refunds within 30 days.", [Step(index=0, tool="search_policies", ok=True)])
    s = tiered(case, good, score(case, good), JudgeVerdict(score=5, resolved=True, reason="ok"))
    assert all(s.tiers[t.value].passed for t in Tier) and s.judge_score == 5.0
    assert s.tiers["system"].metrics["resolved_without_escalation"] is True

    escalated = _traj(
        "Refunds within 30 days.",
        [Step(index=0, tool="search_policies", ok=True)],
        proposed=[{"tier": "billing"}],
    )
    s = tiered(case, escalated, score(case, escalated), None)
    assert not s.tiers["system"].passed and s.tiers["turn"].passed
    assert "proposed escalation" in s.tiers["system"].failures

    judged_low = tiered(
        case, good, score(case, good), JudgeVerdict(score=2, resolved=False, reason="thin")
    )
    assert not judged_low.tiers["turn"].passed and judged_low.tiers["system"].passed
    assert judged_low.tiers["turn"].failures[0].startswith("judge scored the reply 2/5")

    capped = _traj(
        "Stopped",
        [Step(index=0, tool="search_policies", ok=True)],
        terminated=Termination.MAX_STEPS,
    )
    s = tiered(case, capped, score(case, capped), None)
    assert (
        not s.tiers["session"].passed and "terminated by max_steps" in s.tiers["session"].failures
    )
    assert not s.tiers["system"].passed  # a capped run did not resolve the ticket

    must = _case({"must_escalate": True, "escalate_tier_any": ["security"]})
    right = _traj(
        "Escalated.", [Step(index=0, tool="classify_urgency", ok=True)], [{"tier": "security"}]
    )
    s = tiered(must, right, score(must, right), None)
    assert s.tiers["system"].passed and s.tiers["system"].metrics["escalation_required"]
    assert s.tiers["system"].metrics["resolved_without_escalation"] is False


async def test_scripted_judge_follows_the_expectations():
    case = _case({"must_not_escalate": True, "final_must_contain_any": ["30 days"]})
    v = await scripted_judge(case, _traj("Refunds within 30 days.", []))
    assert v.score == 5 and v.resolved
    v = await scripted_judge(case, _traj("No idea.", []))
    assert v.score == 2 and not v.resolved


async def test_llm_judge_uses_the_judge_role_and_the_registered_prompt(make_client):
    provider = FakeProvider(['{"score": 4, "resolved": true, "reason": "cites the policy"}'])
    client = make_client(provider)
    case = _case({})
    v = await llm_judge(client)(case, _traj("Refunds within 30 days.", []))
    assert v.score == 4 and v.resolved
    call = provider.calls[0]
    assert call["model"] == client.model_for(__import__("nw.config").config.ModelRole.JUDGE)
    assert call["system"].startswith(JUDGE_PROMPT.text)
    assert JUDGE_PROMPT.version.startswith("agent.judge_turn@")


@pytest.fixture
def offline(monkeypatch, tmp_path, local_settings):
    import nw.agent.northwind as nwmod
    from nw.agent.offline import offline_registry, scripted_provider

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    cases = load_cases(*CASE_FILES)
    registry = offline_registry(ROOT / "data" / "accounts.json")
    client = LLMClient(scripted_provider(cases), settings=local_settings)
    return cases, registry, client


async def test_offline_run_labels_every_case_with_four_tiers_and_passes_the_tier_bars(
    offline, tmp_path
):
    cases, registry, client = offline
    scores, agg = await evaluate(
        cases, registry, client, trace_dir=tmp_path / "traces", judge=scripted_judge
    )
    assert all(set(s.tiers) == {"tool", "turn", "session", "system"} for s in scores)
    tiers = agg["tiers"]
    n = len(cases)
    assert all(tiers[t.value]["passed"] == n and tiers[t.value]["n"] == n for t in Tier)
    assert tiers["turn"]["judge_mean"] == 5.0 and tiers["turn"]["judged"] == n
    assert tiers["tool"]["invalid_argument_rate"] == 0.0 and tiers["tool"]["per_tool"]
    assert tiers["session"]["cap_rate"] == 0.0
    assert tiers["system"]["escalation_correct_rate"] == 1.0
    assert tiers["system"]["resolved_without_escalation_rate"] == 1.0
    assert 0 < tiers["system"]["escalation_rate"] < 1
    baseline = load_baseline(ROOT / BASELINE)
    assert set(baseline["tiers"]) == {"tool", "turn", "session", "system"}
    d = gate(agg, scores, baseline)
    assert d.passed, d.reasons
    assert d.tiers == {"tool": True, "turn": True, "session": True, "system": True}
    report = format_report(scores, agg)
    assert "| Tier | Cases | Detail |" in report and f"| tool | {n}/{n} |" in report
    assert "| Tool | Calls | Errors | Error rate |" in report


async def test_no_judge_leaves_the_turn_tier_unjudged(offline, tmp_path):
    cases, registry, client = offline
    scores, agg = await evaluate(cases, registry, client, trace_dir=tmp_path / "traces")
    assert agg["tiers"]["turn"]["judged"] == 0 and agg["tiers"]["turn"]["judge_mean"] is None
    assert all(s.judge_score is None for s in scores)
    assert gate(agg, scores, load_baseline(ROOT / BASELINE)).passed


# ----- the gate's tier bars ---------------------------------------------------------


def _scores(n=15, **over):
    out = []
    for i in range(1, n + 1):
        kind = "injection" if i in (1, 2) else "other"
        per_tool = {"search_policies": {"calls": 2, "errors": 0, "invalid": 0}}
        s = CaseScore(
            id=f"adv-{i:02d}",
            kind=kind,
            success=True,
            failures=[],
            steps=5,
            tool_calls=2,
            tool_precision=0.8,
            cost_usd=0.10,
            terminated="answer",
            escalated=False,
            unapproved_execution=False,
            trace="t",
            judge_score=over.get("judge", 5.0),
            tiers={
                "tool": TierScore(
                    passed=True,
                    metrics={
                        "calls": 2,
                        "invalid_arguments": over.get("invalid", 0),
                        "errors": over.get("errors", 0),
                        "per_tool": over.get("per_tool", per_tool),
                    },
                ),
                "turn": TierScore(passed=over.get("turn_pass", True)),
                "session": TierScore(passed=True),
                "system": TierScore(
                    passed=True,
                    metrics={
                        "escalation_correct": over.get("esc_ok", True),
                        "escalation_required": False,
                        "escalated": False,
                        "resolved_without_escalation": True,
                        "cost_usd": 0.10,
                    },
                ),
            },
        )
        if over.get("capped"):
            s.terminated = "max_steps"
        out.append(s)
    return out


def _baseline():
    s = _scores()
    return baseline_from(aggregate(s, "base00000000"), s, "test")


def test_baseline_carries_the_tier_bars_and_the_policy_reads_them():
    b = _baseline()
    assert b["tiers"] == DEFAULT_TIER_BARS and b["aggregate"]["tiers"]["turn"]["judge_mean"] == 5.0
    committed = load_baseline(ROOT / BASELINE)
    p = GatePolicy.from_baseline(committed)
    assert p.max_pass_drop == 1 and p.max_cost_ratio == 1.5 and p.max_extra_steps == 2.0
    assert p.bars["turn"]["min_judge_mean"] == 3.5
    tightened = {**committed, "tiers": {**committed["tiers"], "session": {"max_pass_drop": 0}}}
    assert GatePolicy.from_baseline(tightened).max_pass_drop == 0


def test_each_tier_bar_names_its_tier():
    b = _baseline()
    bad_tool = _scores(
        errors=1, per_tool={"search_policies": {"calls": 2, "errors": 1, "invalid": 0}}
    )
    d = gate(aggregate(bad_tool), bad_tool, b)
    assert not d.passed and d.tiers["tool"] is False and d.tiers["turn"] is True
    assert d.reasons[0].startswith("[tool] search_policies failed 15 of 30 calls")

    invalid = _scores(invalid=1)
    d = gate(aggregate(invalid), invalid, b)
    assert any(r.startswith("[tool] invalid argument rate") for r in d.reasons)

    low_judge = _scores(judge=3.0)
    d = gate(aggregate(low_judge), low_judge, b)
    assert d.reasons == ["[turn] judge mean 3.00 is under 3.5"]

    turns = _scores(turn_pass=False)
    d = gate(aggregate(turns), turns, b)
    assert any(r.startswith("[turn] 0/15 turns passed") for r in d.reasons)

    capped = _scores(capped=True)
    d = gate(aggregate(capped), capped, b)
    assert any(r.startswith("[session] 1.00 of runs ended on a cap") for r in d.reasons)

    wrong = _scores(esc_ok=False)
    d = gate(aggregate(wrong), wrong, b)
    assert any(r.startswith("[system] escalation correct on 0.00") for r in d.reasons)
    assert "tiers tool pass, turn pass, session pass, system FAIL" in format_decision(d)


def test_relative_bars_are_labelled_session_and_system():
    b = _baseline()
    pricey = _scores()
    for s in pricey:
        s.cost_usd = 0.2
    d = gate(aggregate(pricey), pricey, b)
    assert any(r.startswith("[system] cost per resolution") for r in d.reasons)
    slow = _scores()
    for s in slow:
        s.steps = 9
    d = gate(aggregate(slow), slow, b)
    assert any(r.startswith("[session] mean steps") for r in d.reasons)
    # A baseline without a tiers block still gates, on the default bars, and says so.
    legacy = {k: v for k, v in b.items() if k != "tiers"}
    d = gate(aggregate(_scores()), _scores(), legacy)
    assert d.passed and any("no tiers block" in n for n in d.notes)


def test_cli_offline_gate_reports_tiers(monkeypatch, tmp_path, capsys):
    import nw.agent.northwind as nwmod
    from nw.agent import evaluate as cli

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    monkeypatch.chdir(ROOT)
    out = tmp_path / "agent_eval.json"
    argv = [
        "evaluate",
        "--provider",
        "fake",
        "--gate",
        "--out",
        str(out),
        "--traces",
        str(tmp_path / "traces"),
        "--baseline",
        str(ROOT / BASELINE),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    printed = capsys.readouterr().out
    assert "tiers tool pass, turn pass, session pass, system pass" in printed
    report = json.loads(out.read_text())
    assert report["aggregate"]["tiers"]["turn"]["judge_mean"] == 5.0
    assert report["scores"][0]["tiers"]["system"]["passed"] is True
    decision = json.loads((tmp_path / "agent_gate.jsonl").read_text().splitlines()[-1])
    assert decision["tiers"] == {"tool": True, "turn": True, "session": True, "system": True}
    monkeypatch.setattr(sys, "argv", [*argv, "--no-judge"])
    assert cli.main() == 0
    assert "not judged" in capsys.readouterr().out
