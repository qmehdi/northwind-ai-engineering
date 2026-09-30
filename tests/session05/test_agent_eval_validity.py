"""AgentOps evaluation validity: a gate that refuses baselines without provenance, pass^k over
repeated runs, representative benign cases, a calibrated turn Judge, deterministic record and
replay, and a drift monitor that never compares live costs with a scripted or Claude-era run."""

import json
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from nw.agent import evaluate as ev
from nw.agent.evaluate import (
    CaseScore,
    GatePolicy,
    JudgeVerdict,
    RecordingProvider,
    ReplayProvider,
    aggregate,
    baseline_from,
    calibrate_turn_judge,
    evaluate,
    gate,
    load_cases,
    load_turn_calibration,
    provenance_problems,
)
from nw.agent.monitor import AgentMonitor, JudgeSampler, baseline_problem
from nw.llm import LLMClient

pytestmark = pytest.mark.session05
ROOT = Path(__file__).resolve().parents[2]
CASE_FILES = [ROOT / p for p in ev.DEFAULT_CASES]
PROV = {
    "mode": "live",
    "track": "aws",
    "provider": "settings",
    "models": {"workhorse": "openai.gpt-oss-120b-1:0", "workhorse_eu": "x-eu", "judge": "j"},
    "judge": "llm",
    "cases_sha256_12": "abc",
    "repeats": 3,
}


def _score(cid, ok=True, attempt=0, kind="other"):
    return CaseScore(
        id=cid,
        kind=kind,
        success=ok,
        failures=[] if ok else ["x"],
        steps=4,
        tool_calls=2,
        tool_precision=1.0,
        cost_usd=0.01,
        terminated="answer",
        escalated=False,
        unapproved_execution=False,
        trace="t",
        attempt=attempt,
    )


# ----- pass^k ---------------------------------------------------------------------------
def test_pass_hat_k_counts_a_case_only_when_every_run_passed():
    scores = [_score("a", True, i) for i in range(3)] + [
        _score("b", ok, i) for i, ok in enumerate((True, False, True))
    ]
    agg = aggregate(scores)
    assert agg["n"] == 2 and agg["runs"] == 6 and agg["repeats"] == 3
    assert agg["success"] == 1 and agg["pass_hat_k"] == 0.5 and agg["pass_at_k"] == 1.0
    assert agg["success_runs"] == 5 and agg["success_ci"]["n"] == 2
    b = baseline_from(agg | {"provenance": PROV}, scores, "t")
    assert b["cases"]["b"]["runs"] == [True, False, True] and b["provenance"] == PROV


async def test_repeats_run_every_case_k_times(monkeypatch, tmp_path, local_settings):
    import nw.agent.northwind as nwmod
    from nw.agent.offline import offline_registry, scripted_provider

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "esc.jsonl")
    cases = load_cases(*CASE_FILES)[:4]
    client = LLMClient(scripted_provider(cases), settings=local_settings)
    scores, agg = await evaluate(
        cases,
        offline_registry(ROOT / "data" / "accounts.json"),
        client,
        trace_dir=tmp_path,
        repeats=3,
        provenance=PROV,
    )
    assert len(scores) == 12 and agg["n"] == 4 and agg["repeats"] == 3
    assert agg["provenance"]["repeats"] == 3 and agg["provenance"]["agent_version"]


# ----- provenance -----------------------------------------------------------------------
def test_the_gate_refuses_legacy_and_unprovenanced_baselines():
    scores = [_score(f"c{i}") for i in range(5)]
    agg = aggregate(scores, "v1") | {"provenance": PROV}
    legacy = json.loads((ROOT / "data" / "golden" / "agent_baseline.json").read_text())
    d = gate(agg, scores, legacy)
    assert not d.passed and any("legacy" in r for r in d.reasons)
    bare = baseline_from(aggregate(scores, "v1"), scores, "t")
    bare["provenance"] = None
    d = gate(agg, scores, bare)
    assert not d.passed and any("has no provenance" in r for r in d.reasons)
    same = baseline_from(aggregate(scores, "v1"), scores, "t", provenance=PROV)
    assert gate(agg, scores, same).passed


def test_model_track_cases_and_repeats_are_comparability():
    base = {"provenance": PROV}
    moved = {**PROV, "models": {**PROV["models"], "workhorse": "anthropic.claude-sonnet-5"}}
    fails, _ = provenance_problems(moved, base, GatePolicy())
    assert fails and "workhorse model differs" in fails[0]
    fails, notes = provenance_problems(moved, base, GatePolicy(allow_model_change=True))
    assert not fails and "deliberate" in notes[0]
    for key, value in (("track", "gcp"), ("cases_sha256_12", "def"), ("repeats", 1)):
        assert provenance_problems({**PROV, key: value}, base, GatePolicy())[0], key
    no_judge = {**PROV, "judge": "none", "models": {**PROV["models"], "judge": "none"}}
    fails, notes = provenance_problems(no_judge, base, GatePolicy())
    assert not fails and "no turn judge" in notes[0]


def test_an_uncalibrated_turn_judge_is_reported_not_gated():
    scores = [_score(f"c{i}") for i in range(5)]
    for s in scores:
        s.tiers = {}
    agg = aggregate(scores, "v1")
    agg["tiers"] = {
        "tool": {"n": 5, "passed": 5, "pass_rate": 1, "calls": 10, "invalid_argument_rate": 0,
                 "error_rate": 0, "per_tool": {}},
        "turn": {"n": 5, "passed": 5, "pass_rate": 1.0, "judged": 5, "judge_mean": 3.0,
                 "tool_precision": 1.0},
        "session": {"n": 5, "passed": 5, "pass_rate": 1, "cap_rate": 0, "mean_steps": 4,
                    "unapproved_executions": 0},
        "system": {"n": 5, "passed": 5, "pass_rate": 1, "escalation_correct_rate": 1.0,
                   "escalation_rate": 0, "resolved_without_escalation_rate": 1,
                   "cost_usd_per_resolution": 0.01},
    }  # fmt: skip
    assert not gate(agg, scores, None).passed, "a calibrated judge mean of 3.0 fails the turn"
    d = gate(agg | {"judge_gated": False}, scores, None)
    assert d.passed and any("no calibration report" in n for n in d.notes)


# ----- the turn Judge's calibration -------------------------------------------------------
async def test_turn_judge_calibration_measures_the_decision_and_the_mean():
    cases = load_turn_calibration()
    assert len(cases) >= 40 and sum(c.human_score < 3 for c in cases) >= 15

    async def generous(c):
        return JudgeVerdict(score=min(5, c.human_score + 1), resolved=c.human_resolved, reason="r")

    r = await calibrate_turn_judge(cases, generous)
    assert r["n"] == len(cases) and r["mean_score"]["bias"] > 0
    assert r["false_pass"]["k"] == sum(1 for c in cases if c.human_score == 2)
    assert "bias" in ev.format_calibration(r | {"judge_model": "j"})
    assert r["resolved_agreement"]["rate"] == 1.0


def test_judge_calibration_report_is_matched_by_model_id(tmp_path):
    path = tmp_path / "cal.json"
    assert not ev.judge_calibrated("claude-opus-5", path)
    path.write_text(json.dumps({"judge_model": "claude-opus-5"}))
    assert ev.judge_calibrated("claude-opus-5", path) and not ev.judge_calibrated("x", path)


def test_a_shared_turn_judge_calibration_serves_the_cohort(tmp_path):
    own, shared = tmp_path / "cal.json", tmp_path / "calibrations"
    shared.mkdir()
    assert not ev.judge_calibrated("claude-opus-5", own, shared)
    (shared / "agent-judge-gcp.json").write_text(json.dumps({"judge_model": "claude-opus-5"}))
    assert ev.judge_calibrated("claude-opus-5", own, shared)
    assert not ev.judge_calibrated("x", own, shared)
    assert not ev.judge_calibrated("claude-opus-5", own)  # a non-default path reads no share


# ----- record and replay ----------------------------------------------------------------
async def test_a_recorded_run_replays_to_the_same_scores(monkeypatch, tmp_path, local_settings):
    import nw.agent.northwind as nwmod
    from nw.agent.offline import offline_registry, scripted_provider

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "esc.jsonl")
    cases = load_cases(*CASE_FILES)[:6]
    registry = offline_registry(ROOT / "data" / "accounts.json")
    cassette = tmp_path / "cassette.jsonl"
    recorder = RecordingProvider(scripted_provider(cases), cassette, header={"judge": "none"})
    first, a1 = await evaluate(
        cases, registry, LLMClient(recorder, settings=local_settings), trace_dir=tmp_path / "a"
    )
    replay = ReplayProvider(cassette)
    assert replay.header == {"judge": "none"} and len(replay.tape) == len(cases)
    second, a2 = await evaluate(
        cases, registry, LLMClient(replay, settings=local_settings), trace_dir=tmp_path / "b"
    )
    key = lambda s: (s.id, s.success, s.steps, s.tool_calls, s.escalated)  # noqa: E731
    assert sorted(map(key, first)) == sorted(map(key, second))
    assert a1["success"] == a2["success"] == len(cases)
    diverged = ReplayProvider(cassette)
    diverged.tape = {}
    _, a3 = await evaluate(
        cases[:1], registry, LLMClient(diverged, settings=local_settings), trace_dir=tmp_path / "c"
    )
    assert a3["success"] == 0, "a call the recording never made fails the case"


def test_cli_replays_a_cassette_offline(monkeypatch, tmp_path, capsys):
    import nw.agent.northwind as nwmod
    from nw.agent.offline import scripted_provider

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "esc.jsonl")
    monkeypatch.chdir(ROOT)
    cases = load_cases(*CASE_FILES)
    rec = RecordingProvider(
        scripted_provider(cases), tmp_path / "c.jsonl", header={"judge": "none"}
    )
    import asyncio

    from nw.agent.offline import offline_registry

    async def record():
        from nw.config import settings

        return await evaluate(
            cases, offline_registry(), LLMClient(rec, settings=settings()), trace_dir=tmp_path / "r"
        )

    asyncio.run(record())
    argv = ["evaluate", "--provider", "replay", "--replay", str(tmp_path / "c.jsonl"), "--out",
            str(tmp_path / "e.json"), "--traces", str(tmp_path / "t")]  # fmt: skip
    monkeypatch.setattr(sys, "argv", argv)
    assert ev.main() == 0
    assert f"{len(cases)}/{len(cases)} cases succeeded" in capsys.readouterr().out


# ----- drift ---------------------------------------------------------------------------------
def test_monitor_refuses_legacy_offline_and_other_model_baselines():
    legacy = json.loads((ROOT / "data" / "golden" / "agent_baseline.json").read_text())
    assert "legacy" in baseline_problem(legacy)
    offline = json.loads(
        (ROOT / "data" / "golden" / "baselines" / "agent-offline.json").read_text()
    )
    assert "offline" in baseline_problem(offline)
    live = {
        "provenance": {"mode": "live", "models": {"workhorse": "a"}},
        "steps": [4],
        "costs": [0.1],
    }
    assert baseline_problem(live, "a") is None and "measured with a" in baseline_problem(live, "b")
    m = AgentMonitor(legacy)
    assert not m.enabled and m.min_window == 200 and "legacy" in m.snapshot().psi_off


def test_sampled_judge_scores_drive_the_quality_gauge():
    m = AgentMonitor(None)
    for _ in range(25):
        m.observe_judge(2.5)
    snap = m.snapshot()
    assert snap.quality_level == "alert" and snap.judged == 25
    assert REGISTRY.get_sample_value("nw_agent_judge_score") == pytest.approx(2.5)
    assert REGISTRY.get_sample_value("nw_agent_quality_level") == 2
    sampler = JudgeSampler(0.25, seed=1)
    picks = sum(sampler.should_sample() for _ in range(1000))
    assert 200 < picks < 300 and not JudgeSampler(0.0).should_sample()


async def test_the_service_judges_a_sample_of_live_runs_off_the_request_path(
    make_agent_app, monkeypatch
):
    from nw.agent import service
    from nw.agent.loop import scripted_completion
    from nw.llm.providers.fake import FakeProvider

    app = make_agent_app(
        FakeProvider([scripted_completion("Your plan includes SSO.")]),
        sampler=JudgeSampler(1.0, seed=0),
        monitor=AgentMonitor(None),
    )
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/run", json={"task": "Ticket T-200001 from account NW-10007: SSO?"})
        assert r.status_code == 200
        for t in list(service.state.judge_tasks):
            await t
    assert service.state.monitor.judge_scores and service.state.monitor.judge_scores[-1] == 4


def test_the_service_reads_the_sample_rate_and_this_tracks_baseline(monkeypatch, tmp_path):
    from nw.agent import service

    monkeypatch.setattr(service, "state", service.State())
    monkeypatch.setenv("NW_AGENT_JUDGE_SAMPLE", "0.25")
    monkeypatch.delenv("NW_AGENT_BASELINE", raising=False)
    monkeypatch.chdir(tmp_path)
    service.configure_agentops()
    assert service.state.sampler.rate == 0.25 and service.state.monitor.min_window == 200
    assert service.state.monitor.psi_off == "no baseline"
