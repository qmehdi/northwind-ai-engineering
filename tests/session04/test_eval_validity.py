"""Evaluation validity for Project 3: answer correctness against the gold answer, citation
recall, recall at 1 and 3, per-slice numbers with their sizes, a held-out gate split, and a
gate that refuses comparisons it cannot trust and counts flipped cases instead of trusting a
tolerance smaller than one case."""

import argparse
import json
from pathlib import Path

import pytest

from nw.llm.providers.fake import FakeProvider
from nw.policy import evaluate as ev
from nw.policy.answer import Answer
from nw.policy.calibrate import (
    CASES,
    CalibrationCase,
    calibrate,
    candidates_from_eval,
    format_report,
)
from nw.policy.calibrate import (
    load_cases as load_calibration,
)
from nw.policy.chunking import chunk_corpus
from nw.policy.evaluate import (
    EvalCase,
    EvalPolicy,
    baseline_path,
    citation_recall,
    decide,
    derive_key_facts,
    evaluate,
    key_fact_correctness,
    provenance_problems,
    select,
)
from nw.policy.monitor import PolicyDriftMonitor, make_baseline
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex
from tests.session04.test_answer import draft

pytestmark = pytest.mark.session04
ROOT = Path(__file__).resolve().parents[2]


# ----- deterministic answer metrics ---------------------------------------------------
def test_key_facts_come_from_the_gold_answer_and_survive_rewording():
    facts = derive_key_facts("Business keeps audit logs for 90 days, Enterprise for 400 days.")
    assert facts == ["90 days", "400 days"]
    case = EvalCase(id="1", question="q", gold_answer="Kept for 90 days, or 400 days.")
    assert key_fact_correctness(case, "A 90-day retention; Enterprise: 400 days.") == (1.0, False)
    assert key_fact_correctness(case, "Kept for 90 days.")[0] == 0.5
    stale = EvalCase(id="2", question="q", key_facts=["30 days"], stale_facts=["14 days"])
    assert key_fact_correctness(stale, "Within 14 days of renewal.") == (0.0, True)
    alt = EvalCase(id="3", question="q", key_facts=[["99.95", "99,95"]])
    assert key_fact_correctness(alt, "Verfügbarkeit von 99,95 Prozent")[0] == 1.0
    assert key_fact_correctness(EvalCase(id="4", question="q"), "anything") == (None, False)


def test_citation_recall_counts_gold_chunks_cited():
    a = Answer(text="t", citations=["a#1"], confidence=1, refused=False, context_ids=["a#1"])
    assert citation_recall(a, ["a#1", "b#2"]) == 0.5 and citation_recall(a, []) is None


@pytest.fixture
def index(corpus_dir):
    return PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())


async def test_report_has_recall_at_1_and_3_intervals_and_slices(index, make_client):
    good = index.retrieve("Enterprise uptime", k=3)[0].chunk.id
    cases = [
        EvalCase(
            id="1",
            question="Enterprise uptime?",
            gold_chunk_ids=[good],
            gold_answer="Enterprise gets 99.95 percent.",
            slices=["currency"],
        ),
        EvalCase(id="2", question="Office colour?", must_refuse=True, language="de"),
    ]
    provider = FakeProvider(
        [draft("Enterprise gets 99.95 percent.", [good]), draft("", [], answerable=False)]
    )
    results, agg = await evaluate(
        make_client(provider, max_concurrency=1), index, cases, k=3, concurrency=1, use_judge=False
    )
    r1 = next(r for r in results if r.id == "1")
    assert r1.recall_at_1 == 1.0 and r1.key_fact_correctness == 1.0 and r1.citation_recall == 1.0
    assert agg["recall_at_1"] == pytest.approx(1.0) and agg["key_fact_correctness"] == 1.0
    assert agg["intervals"]["refusal_correct"]["n"] == 2
    slices = agg["by_slice"]
    assert slices["slice:currency"]["n"] == 1 and slices["language:de"]["n"] == 1
    assert "Recall@1" in ev.format_report(agg) and "| slice:currency | 1 |" in ev.format_report(agg)


def test_split_selection_keeps_the_gate_set_held_out():
    cases = [EvalCase(id=str(i), question="q", split="dev" if i % 2 else "gate") for i in range(6)]
    assert [c.id for c in select(cases, "gate")] == ["0", "2", "4"]
    assert len(select(cases, "all")) == 6 and len(select(cases, "dev")) == 3


# ----- provenance ----------------------------------------------------------------------
def _report(mode="no_judge", **over):
    r = {
        "mode": mode,
        "track": "aws",
        "split": "gate",
        "golden_sha256_12": "g1",
        "corpus_sha256_12": "c1",
        "prompt_versions": {"policy.answer": "a1", "policy.judge": "j1"},
        "models": {"workhorse": "openai.gpt-oss-120b-1:0", "judge": "claude-opus-5"},
        "index_manifest": {"embedder": "e", "reranker": "r"},
        "aggregate": {
            "recall_at_k": 1.0,
            "mrr": 1.0,
            "key_fact_correctness": 1.0,
            "citation_recall": 1.0,
            "citation_validity": 1.0,
            "faithfulness": 1.0,
        },
        "results": [
            {
                "id": f"c{i}",
                "recall_at_k": 1.0,
                "recall_at_1": 1.0,
                "refusal_correct": True,
                "mrr": 1.0,
                "key_fact_correctness": 1.0,
                "citation_recall": 1.0,
                "citation_validity": 1.0,
                "faithfulness": 1.0,
            }
            for i in range(10)
        ],
    }
    r.update(over)
    return r


def test_legacy_and_unprovenanced_baselines_fail_the_gate():
    legacy = json.loads((ROOT / "data" / "golden" / "baseline.json").read_text())
    d = decide(_report(), legacy)
    assert not d.passed and "legacy" in d.reasons[0]
    d = decide(_report(), {"aggregate": {"mrr": 1.0}, "results": []})
    assert not d.passed and "has no mode" in d.reasons[0]


def test_model_ids_are_part_of_comparability():
    other = _report(models={"workhorse": "anthropic.claude-sonnet-5", "judge": "claude-opus-5"})
    fails, _ = provenance_problems(_report(), other)
    assert fails and "workhorse model differs" in fails[0]
    fails, notes = provenance_problems(_report(), other, allow_model_change=True)
    assert not fails and "deliberate" in notes[0]
    judged = _report("judged")
    new_judge = _report("judged", models={"workhorse": "openai.gpt-oss-120b-1:0", "judge": "x"})
    assert any("judge model differs" in f for f in provenance_problems(judged, new_judge)[0])
    prompt = _report("judged", prompt_versions={"policy.answer": "a1", "policy.judge": "j2"})
    assert any("Judge prompt" in f for f in provenance_problems(judged, prompt)[0])
    # A changed answer prompt is what the gate measures: a note, not a refusal.
    answer = _report(prompt_versions={"policy.answer": "a2", "policy.judge": "j1"})
    d = decide(answer, _report())
    assert d.passed and any("policy.answer changed" in n for n in d.notes)
    assert not decide(_report(track="gcp"), _report()).passed


def test_one_flipped_case_passes_two_fail_with_mcnemar_evidence():
    base = _report()
    one = _report()
    one["results"][0] = dict(one["results"][0], refusal_correct=False)
    d = decide(one, base)
    assert d.passed and d.evidence["refusal_correct"]["champion_only"] == 1
    two = _report()
    for i in (0, 1):
        two["results"][i] = dict(two["results"][i], refusal_correct=False)
    d = decide(two, base)
    assert not d.passed and "refusal_correct: 2 cases lost" in d.reasons[0]
    assert decide(two, base, EvalPolicy(max_case_flips=2)).passed


def test_an_uncalibrated_judge_is_reported_not_gated():
    base = _report("judged")
    cur = _report("judged", aggregate=dict(_report()["aggregate"], faithfulness=0.5))
    d = decide(cur, base)
    assert d.passed and any("no calibration report" in n for n in d.notes)
    cur["judge_calibration"] = {"matches_judge": True}
    assert not decide(cur, base).passed
    del cur["judge_calibration"]
    assert not decide(cur, base, EvalPolicy(require_calibrated_judge=True)).passed


def test_a_shared_calibration_for_the_same_judge_is_used(tmp_path):
    """The Judge is one model per track: a committed report calibrated once (the instructor,
    in cohort mode) gates every checkout that has none of its own, and only for that id."""
    own, shared = tmp_path / "own.json", tmp_path / "calibrations"
    shared.mkdir()
    assert not ev.judge_calibration("claude-opus-5", own, shared)["matches_judge"]
    (shared / "policy-judge-aws.json").write_text(
        json.dumps({"judge_model": "claude-opus-5", "n": 102, "kappa": 0.8})
    )
    found = ev.judge_calibration("claude-opus-5", own, shared)
    assert found["matches_judge"] and found["n"] == 102 and found["path"].endswith("aws.json")
    assert not ev.judge_calibration("another-judge", own, shared)["matches_judge"]
    own.write_text(json.dumps({"judge_model": "claude-opus-5", "n": 7}))
    assert ev.judge_calibration("claude-opus-5", own, shared)["n"] == 7  # your own wins
    assert not ev.judge_calibration("claude-opus-5", tmp_path / "none.json")["found"]


def test_baselines_live_per_track_and_mode():
    assert baseline_path("aws", "retrieval_only").name == "policy-retrieval_only.json"
    assert baseline_path("gcp", "no_judge").name == "policy-gcp-no_judge.json"


async def test_cli_writes_a_provenanced_baseline_then_gates_against_it(
    corpus_dir, tmp_path, monkeypatch
):
    import nw.policy.retrieval as retrieval
    from nw.policy.build_index import build

    monkeypatch.setattr(retrieval, "real_embeddings", HashEmbeddings)
    monkeypatch.setattr(retrieval, "CrossEncoderReranker", OverlapReranker)
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps({"id": "g1", "question": "Enterprise uptime?", "split": "gate"})
        + "\n"
        + json.dumps({"id": "g2", "question": "tune me", "split": "dev"})
        + "\n"
    )
    out = tmp_path / "index"
    build(
        corpus_dir,
        out,
        embeddings=HashEmbeddings(),
        reranker=OverlapReranker(),
        baseline_run=None,
        golden=golden,
    )
    baseline = tmp_path / "baselines" / "b.json"
    args = argparse.Namespace(
        golden=golden,
        index=out,
        chunks=out / "chunks.jsonl",
        baseline=str(baseline),
        corpus=corpus_dir,
        out=tmp_path / "eval.json",
        k=3,
        min_score=0.0,
        hybrid=True,
        rerank=True,
        judge=False,
        retrieval_only=True,
        split="gate",
        write_baseline=True,
        require_baseline=False,
        gate_log=tmp_path / "gate.jsonl",
    )
    assert await ev.main_async(args) == 0 and baseline.exists()
    written = json.loads(baseline.read_text())
    assert written["aggregate"]["n"] == 1 and written["split"] == "gate"
    for key in ev.PROVENANCE:
        assert written.get(key) not in (None, {}, ""), key
    args.write_baseline = False
    assert await ev.main_async(args) == 0
    log = [json.loads(x) for x in (tmp_path / "gate.jsonl").read_text().splitlines()]
    assert log[-1]["passed"] and log[-1]["models"]["embedder"] == "hash"
    args.baseline = str(tmp_path / "missing.json")
    args.require_baseline = True
    assert await ev.main_async(args) == 1

    # A baseline the gate cannot compare with (here: another split) is not overwritten
    # silently; --rebase with a reason replaces it and records what it replaced.
    args.require_baseline = False
    args.baseline = str(baseline)
    args.split = "dev"
    args.write_baseline = True
    assert await ev.main_async(args) == 1
    assert json.loads(baseline.read_text())["split"] == "gate"
    args.rebase = "new golden split for the drill"
    await ev.main_async(args)
    rebased = json.loads(baseline.read_text())
    assert rebased["split"] == "dev"
    assert rebased["rebased"]["reason"] == "new golden split for the drill"
    assert rebased["rebased"]["previous"]["split"] == "gate" and rebased["rebased"]["gate_reasons"]


# ----- the committed golden set and baseline --------------------------------------------
def test_committed_golden_set_has_the_slices_the_gate_needs():
    cases = ev.load_cases(ROOT / "data" / "golden" / "policy_qa.jsonl")
    gate_cases = select(cases, "gate")
    assert len(cases) >= 100 and len(gate_cases) >= 60
    assert sum(c.must_refuse for c in cases) >= 25 and sum(c.language == "de" for c in cases) >= 15
    assert sum(c.must_refuse for c in gate_cases) >= 15
    assert any("multi_hop" in c.slices for c in gate_cases)
    assert len({c.id for c in cases}) == len(cases)


def test_committed_retrieval_baseline_is_provenanced_and_current():
    path = ROOT / "data" / "golden" / "baselines" / "policy-retrieval_only.json"
    b = json.loads(path.read_text())
    assert b["mode"] == "retrieval_only" and b["split"] == "gate" and not b.get("legacy")
    assert b["golden_sha256_12"] == ev.golden_sha(ROOT / "data" / "golden" / "policy_qa.jsonl")
    assert b["index_manifest"]["embedder"] and b["aggregate"]["recall_at_k"] < 1.0


# ----- the Judge's calibration ------------------------------------------------------------
async def test_calibration_reports_intervals_and_the_mean_the_gate_uses():
    cases = load_calibration(CASES)
    assert len(cases) >= 100 and sum(not c.human_faithful for c in cases) >= 40

    async def lenient(q, a, p):
        return 1.0

    r = await calibrate(cases, lenient, threshold=0.75)
    fp = r["intervals"]["false_pass"]
    assert fp["rate"] == 1.0 and fp["n"] == sum(not c.human_faithful for c in cases)
    assert r["mean_score"]["bias"] > 0.4 and r["mean_score"]["bias_ci"][0] > 0
    assert "bias" in format_report(r) and "95% CI" in format_report(r)


def test_live_answers_become_unlabelled_calibration_cases():
    report = {
        "results": [
            {
                "id": "a",
                "answer": "90 days",
                "citations": ["c#1"],
                "refused": False,
                "question": "q",
            },
            {"id": "b", "answer": "", "citations": [], "refused": True},
        ]
    }
    rows = candidates_from_eval(report, {"c#1": "Audit logs 90 days."}, source="gpt-oss")
    assert len(rows) == 1 and rows[0]["human_faithful"] is None and rows[0]["source"] == "gpt-oss"
    CalibrationCase.model_validate({**rows[0], "human_faithful": True})


# ----- drift -------------------------------------------------------------------------------
def test_policy_monitor_names_its_baseline_and_publishes_the_refusal_gauges():
    from prometheus_client import REGISTRY

    baseline = make_baseline([0.9] * 40 + [0.1] * 10, refusal_rate=0.2) | {"source": "golden:x"}
    m = PolicyDriftMonitor(baseline)
    assert m.min_window == 200
    for i in range(200):
        m.observe(0.9 if i % 5 else 0.1, refused=i % 5 == 0, answer_length=100)
    snap = m.snapshot()
    assert snap.quality_level == "ok" and "week of traffic" in snap.notes[0]
    assert REGISTRY.get_sample_value("nw_policy_quality_level") == 0
    for _ in range(200):
        m.observe(0.1, refused=True, answer_length=0)
    snap = m.snapshot()
    assert (
        snap.quality_level == "alert" and REGISTRY.get_sample_value("nw_policy_refusal_ratio") > 2
    )


# ----- the workflows -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["eval-gate.yml", "agent-gate.yml", "retrain-triage.yml", "retrain-semantic.yml"]
)
def test_gate_workflows_pin_actions_and_read_only_tokens(name):
    import re

    import yaml

    path = ROOT / ".github" / "workflows" / name
    text = path.read_text()
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses
    assert yaml.safe_load(text)["permissions"] == {"contents": "read"}
    assert "persist-credentials: false" in text


def test_prompt_changes_run_a_required_generation_gate():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "eval-gate.yml").read_text())
    gen = wf["jobs"]["generation"]
    text = json.dumps(gen)
    assert "nw/llm/prompts/" in text and "--no-judge --require-baseline" in text
    assert gen["permissions"]["id-token"] == "write"
    retrieval = json.dumps(wf["jobs"]["retrieval"])
    assert "--retrieval-only --require-baseline" in retrieval
