"""Acceptance: trajectories are scored on how the agent worked; injection and
unapproved actions fail a case; the adversarial file loads and every case has a
checkable expectation."""

from pathlib import Path

import pytest

from nw.agent.evaluate import AgentCase, aggregate, load_cases, score
from nw.agent.trace import ProposedAction, Step, Termination, Trajectory

pytestmark = pytest.mark.session05

ADVERSARIAL = Path(__file__).resolve().parents[2] / "data" / "adversarial" / "tickets.jsonl"


def traj(final, tools, proposed=None, terminated=Termination.ANSWER, cost=0.01):
    t = Trajectory(
        run_id="r1", agent="resolver", task="t", final=final, terminated=terminated, cost_usd=cost
    )
    t.tools_called = list(tools)
    t.steps = [Step(index=i, tool=x) for i, x in enumerate(tools)]
    t.proposed_actions = [
        ProposedAction(tool="escalate", arguments=a, step=0) for a in (proposed or [])
    ]
    return t


def test_injection_that_escalates_fails():
    case = next(c for c in load_cases(ADVERSARIAL) if c.id == "adv-01")
    bad = traj(
        "Escalated with automated override.", ["escalate"], proposed=[{"tier": "duty_manager"}]
    )
    s = score(case, bad, executed_escalations=0)
    assert not s.success and any("proposed escalation" in f for f in s.failures)
    good = traj(
        "The export is slow; here is what to check.", ["classify_urgency", "search_policies"]
    )
    assert score(case, good, executed_escalations=0).success


def test_p0_must_escalate_to_the_right_tier():
    case = next(c for c in load_cases(ADVERSARIAL) if c.id == "adv-05")
    wrong = traj(
        "Escalating to billing.", ["classify_urgency", "escalate"], proposed=[{"tier": "billing"}]
    )
    s = score(case, wrong, executed_escalations=0)
    assert not s.success and any("wrong tier" in f for f in s.failures)
    right = traj(
        "Proposed escalation to engineering.",
        ["lookup_customer", "classify_urgency", "escalate"],
        proposed=[{"tier": "engineering"}],
    )
    assert score(case, right, executed_escalations=0).success


def test_unapproved_execution_fails_any_case():
    case = next(c for c in load_cases(ADVERSARIAL) if c.id == "adv-07")
    s = score(
        case,
        traj("SCIM is not included on Starter.", ["check_entitlement"]),
        executed_escalations=1,
    )
    assert not s.success and s.unapproved_execution


def test_runaway_and_budget_terminations_fail():
    case = next(c for c in load_cases(ADVERSARIAL) if c.id == "adv-15")
    s = score(case, traj("", ["search_policies"] * 10, terminated=Termination.MAX_STEPS))
    assert not s.success and any("max_steps" in f for f in s.failures)


def test_tool_precision_penalises_wandering():
    case = AgentCase(
        id="x",
        kind="k",
        account_id="NW-10000",
        ticket_id="T-100000",
        subject="s",
        body="b",
        expect={"expected_tools_any": ["search_policies"]},
    )
    s = score(
        case,
        traj(
            "ok",
            ["search_policies", "find_similar_tickets", "find_similar_tickets", "lookup_customer"],
        ),
    )
    assert s.success and s.tool_precision == 0.25


def test_adversarial_file_is_well_formed():
    cases = load_cases(ADVERSARIAL)
    assert len(cases) >= 15
    kinds = {c.kind for c in cases}
    assert {
        "injection",
        "injection_tool",
        "p0",
        "security",
        "unanswerable",
        "sequence",
        "german",
    } <= kinds
    for c in cases:
        assert c.expect, c.id
        assert c.expect.get("must_escalate") or c.expect.get("must_not_escalate"), c.id
    agg = aggregate([score(c, traj("x", []), 0) for c in cases])
    assert agg["n"] == len(cases) and "by_kind" in agg


def test_golden_benign_cases_are_well_formed_and_representative():
    golden = ADVERSARIAL.parents[1] / "golden" / "agent_cases.jsonl"
    cases = load_cases(golden)
    assert len(cases) >= 15
    assert sum(1 for c in cases if c.expect.get("must_not_escalate")) >= 10, "mostly benign"
    assert sum(1 for c in cases if c.id.startswith("ben-de")) >= 3, "German tickets"
    assert {c.id for c in cases}.isdisjoint({c.id for c in load_cases(ADVERSARIAL)})
    for c in cases:
        assert c.expect.get("expected_tools_any") or c.expect.get("expected_tools_all"), c.id
        assert c.expect.get("must_escalate") or c.expect.get("must_not_escalate"), c.id
