"""AgentOps: the regression gate. Relative bars against the committed baseline, absolute
bars that never move, and an offline run through the real loop that passes it for free."""

import json
import sys
from pathlib import Path

import pytest

from nw.agent.evaluate import (
    BASELINE,
    DEFAULT_CASES,
    LEGACY_BASELINE,
    CaseScore,
    GatePolicy,
    aggregate,
    baseline_from,
    cases_sha,
    evaluate,
    format_decision,
    gate,
    load_baseline,
    load_cases,
)
from nw.llm import LLMClient

pytestmark = pytest.mark.session05

ROOT = Path(__file__).resolve().parents[2]
ADVERSARIAL = ROOT / "data" / "adversarial" / "tickets.jsonl"
CASE_FILES = [ROOT / p for p in DEFAULT_CASES]


def _scores(n=15, fails=(), steps=5, cost=0.10, unapproved=False):
    out = []
    for i in range(1, n + 1):
        cid = f"adv-{i:02d}"
        kind = "injection" if i in (1, 2) else ("injection_tool" if i == 11 else "other")
        failed = cid in fails
        out.append(
            CaseScore(
                id=cid,
                kind=kind,
                success=not failed,
                failures=["x"] if failed else [],
                steps=steps,
                tool_calls=3,
                tool_precision=0.7,
                cost_usd=cost,
                terminated="answer",
                escalated=False,
                unapproved_execution=unapproved,
                trace="t",
            )
        )
    return out


def _baseline():
    s = _scores()
    return baseline_from(aggregate(s, "base00000000"), s, "test")


def test_committed_baseline_matches_the_case_files_and_carries_provenance():
    b = load_baseline(ROOT / BASELINE)
    cases = load_cases(*CASE_FILES)
    assert b is not None and not b.get("legacy")
    assert b["aggregate"]["n"] == len(cases) == b["aggregate"]["success"]
    assert set(b["cases"]) == {c.id for c in cases}, "regenerate: make agent-baseline-offline"
    prov = b["provenance"]
    assert prov["mode"] == "offline" and prov["repeats"] == 1 and prov["judge"] == "scripted"
    assert prov["cases_sha256_12"] == cases_sha(CASE_FILES)
    legacy = load_baseline(ROOT / LEGACY_BASELINE)
    assert legacy["legacy"] and "Claude" in legacy["legacy_reason"]


def test_one_case_down_is_variance_two_is_a_regression():
    b = _baseline()
    one = _scores(fails=("adv-05",))
    d = gate(aggregate(one), one, b)
    assert d.passed and any("variance" in n for n in d.notes)
    two = _scores(fails=("adv-05", "adv-09"))
    d = gate(aggregate(two), two, b)
    assert not d.passed and any("variance" in r for r in d.reasons)
    assert gate(aggregate(two), two, b, GatePolicy(max_pass_drop=2)).passed
    assert format_decision(d).startswith("REGRESSION GATE FAILED")


def test_injection_and_unapproved_executions_are_absolute_bars():
    b = _baseline()
    inj = _scores(fails=("adv-02",))  # one case down, but it is an injection case
    d = gate(aggregate(inj), inj, b)
    assert not d.passed and any("injection" in r for r in d.reasons)
    bad = _scores(unapproved=True)
    d = gate(aggregate(bad), bad, b)
    assert not d.passed and any("unapproved" in r for r in d.reasons)
    d = gate(aggregate(bad), bad, None)  # no baseline at all: still fails
    assert not d.passed


def test_cost_and_steps_bars():
    b = _baseline()
    ok = _scores(cost=0.14, steps=7)
    assert gate(aggregate(ok), ok, b).passed
    pricey = _scores(cost=0.16)
    d = gate(aggregate(pricey), pricey, b)
    assert not d.passed and any("cost per resolution" in r for r in d.reasons)
    slow = _scores(steps=8)
    d = gate(aggregate(slow), slow, b)
    assert not d.passed and any("mean steps" in r for r in d.reasons)


def test_case_set_change_and_version_change_are_named():
    b = _baseline()
    s = _scores(n=14)
    d = gate(aggregate(s, "new00000000"), s, b)
    assert not d.passed and any("case set changed" in r for r in d.reasons)
    s = _scores()
    d = gate(aggregate(s, "new00000000"), s, b)
    assert d.passed and any("agent_version" in n for n in d.notes)


def test_first_run_without_a_baseline_passes_on_absolute_bars():
    s = _scores()
    d = gate(aggregate(s), s, None)
    assert d.passed and d.baseline_version is None and "absolute bars only" in d.notes[0]


@pytest.fixture
def offline(monkeypatch, tmp_path, local_settings):
    import nw.agent.northwind as nwmod
    from nw.agent.offline import offline_registry, scripted_provider

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    cases = load_cases(*CASE_FILES)
    registry = offline_registry(ROOT / "data" / "accounts.json")
    client = LLMClient(scripted_provider(cases), settings=local_settings)
    return cases, registry, client


async def test_offline_run_through_the_real_loop_passes_the_gate(offline, tmp_path):
    cases, registry, client = offline
    scores, agg = await evaluate(cases, registry, client, trace_dir=tmp_path / "traces")
    assert agg["success"] == len(cases) and agg["injection_resisted"] and agg["agent_versions"]
    assert agg["unapproved_executions"] == 0 and not (tmp_path / "escalations.jsonl").exists()
    d = gate(agg, scores, load_baseline(ROOT / BASELINE))
    assert d.passed, d.reasons
    assert len(list((tmp_path / "traces").glob("*.json"))) == len(cases)


def test_cli_gate_offline(monkeypatch, tmp_path, capsys):
    """`python -m nw.agent.evaluate --provider fake --gate`: what the no-credentials CI job runs."""
    import nw.agent.northwind as nwmod
    from nw.agent import evaluate as cli

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    monkeypatch.chdir(ROOT)
    out = tmp_path / "agent_eval.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
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
        ],
    )
    assert cli.main() == 0
    printed = capsys.readouterr().out
    n = len(load_cases(*CASE_FILES))
    assert "REGRESSION GATE PASSED" in printed and f"{n}/{n} cases succeeded" in printed
    report = json.loads(out.read_text())
    assert report["aggregate"]["agent_versions"] and (tmp_path / "agent_gate.jsonl").exists()


def test_cli_writes_the_first_baseline(monkeypatch, tmp_path, capsys):
    import nw.agent.northwind as nwmod
    from nw.agent import evaluate as cli

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    monkeypatch.chdir(ROOT)
    baseline = tmp_path / "golden" / "agent_baseline.json"
    argv = [
        "evaluate",
        "--provider",
        "fake",
        "--gate",
        "--out",
        str(tmp_path / "agent_eval.json"),
        "--traces",
        str(tmp_path / "traces"),
        "--baseline",
        str(baseline),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0 and baseline.exists()
    assert "this run becomes the baseline" in capsys.readouterr().out
    b = json.loads(baseline.read_text())
    n = len(load_cases(*CASE_FILES))
    assert b["aggregate"]["success"] == n and len(b["steps"]) == n and b["provenance"]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0  # second run compares with the baseline it just wrote
    printed = capsys.readouterr().out
    assert "REGRESSION GATE PASSED" in printed and "against" in printed
