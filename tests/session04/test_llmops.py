"""The LLMOps loop around Project 3: a versioned prompt on every answer, an index that knows
what it was built from, a cache that cannot serve a stale policy, feedback that becomes golden
cases, and drift watched in the service."""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nw.llm.prompts import register
from nw.llm.providers.fake import FakeProvider
from nw.policy import answer as answer_mod
from nw.policy import evaluate as evaluate_mod
from nw.policy import service
from nw.policy.answer import ANSWER_PROMPT, answer
from nw.policy.build_index import build, check
from nw.policy.cache import ResponseCache, normalise
from nw.policy.evaluate import (
    GATE_KEYS,
    RETRIEVAL_KEYS,
    EvalCase,
    comparability,
    evaluate,
    gate,
)
from nw.policy.feedback import load_feedback, must_refuse_guess, to_golden
from nw.policy.manifest import corpus_sha, is_stale, read_manifest
from nw.policy.monitor import PolicyDriftMonitor, make_baseline, psi
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex
from tests.session04.test_answer import draft

pytestmark = pytest.mark.session04

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "eval-gate.yml"
_CONTEXT_ID = re.compile(r"\[([\w\-]+#[0-9a-f]{12})\]")


def cite_first(messages, kwargs):
    """A scripted Workhorse that cites the first passage it was shown."""
    ids = _CONTEXT_ID.findall(messages[-1].content or "")
    return draft("The policy says so.", ids[:1]) if ids else draft("", [], answerable=False)


@pytest.fixture
def golden(corpus_dir, tmp_path):
    path = tmp_path / "golden.jsonl"
    rows = [
        {"id": "g1", "question": "What uptime does Enterprise get?", "gold_chunk_ids": []},
        {"id": "g2", "question": "How are duplicate charges refunded?", "gold_chunk_ids": []},
        {"id": "g3", "question": "What colour is the office?", "must_refuse": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


@pytest.fixture
def built(corpus_dir, golden, tmp_path):
    out = tmp_path / "index"
    manifest = build(
        corpus_dir,
        out,
        embeddings=HashEmbeddings(),
        reranker=OverlapReranker(),
        golden=golden,
        baseline_run=None,
    )
    return out, manifest


# ----- prompt versions ---------------------------------------------------------------
async def test_every_answer_carries_the_prompt_version(corpus_dir, make_client):
    index = PolicyIndex(chunk_corpus_of(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())
    hits = index.retrieve("Enterprise uptime", k=3)
    client = make_client(FakeProvider([draft("99.95 percent.", [hits[0].chunk.id])]))
    a = await answer(client, "Enterprise uptime?", hits)
    assert a.prompt_version == ANSWER_PROMPT.version and a.prompt_version.startswith(
        "policy.answer@"
    )
    refused = await answer(client, "quantum pricing?", hits, min_score=0.99)
    assert refused.refused and refused.prompt_version == ANSWER_PROMPT.version


async def test_changing_the_prompt_changes_the_version_in_the_report(
    corpus_dir, make_client, monkeypatch
):
    index = PolicyIndex(chunk_corpus_of(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())
    good = index.retrieve("Enterprise uptime", k=3)[0].chunk.id
    cases = [EvalCase(id="1", question="Enterprise uptime?", gold_chunk_ids=[good])]
    before, _ = await evaluate(
        make_client(FakeProvider(cite_first)), index, cases, k=3, use_judge=False
    )
    sharper = register("policy.answer.test", ANSWER_PROMPT.text + "\nAnswer in one sentence.")
    monkeypatch.setattr(answer_mod, "ANSWER_PROMPT", sharper)
    after, _ = await evaluate(
        make_client(FakeProvider(cite_first)), index, cases, k=3, use_judge=False
    )
    assert before[0].prompt_version == ANSWER_PROMPT.version
    assert after[0].prompt_version == sharper.version != before[0].prompt_version
    notes = comparability(
        {"prompt_versions": {"policy.answer": sharper.hash}},
        {"prompt_versions": {"policy.answer": ANSWER_PROMPT.hash}},
    )
    assert notes and "policy.answer changed" in notes[0]


# ----- the manifest and staleness ---------------------------------------------------
def test_build_writes_a_manifest_and_check_sees_the_corpus_move(built, corpus_dir, capsys):
    out, manifest = built
    on_disk = read_manifest(out)
    assert on_disk == manifest
    assert manifest["corpus_sha256_12"] == corpus_sha(corpus_dir)
    assert manifest["chunker"] == {"max_tokens": 350, "overlap": 60}
    assert manifest["embedder"] == "hash" and manifest["reranker"] == "overlap"
    assert manifest["chunks"] == len(load_chunks_of(out)) and manifest["built_at"]
    assert set(manifest["prompt_versions"]) >= {"policy.answer", "policy.judge", "llm.structured"}
    assert manifest["baseline"]["n"] == 3 and manifest["baseline"]["refusal_rate"] == pytest.approx(
        1 / 3
    )
    assert sum(manifest["baseline"]["confidence_hist"]) == pytest.approx(1.0)
    loaded = PolicyIndex.load(out, HashEmbeddings(), out / "chunks.jsonl")
    assert loaded.manifest_hash == manifest["corpus_sha256_12"]
    assert manifest["baseline"]["source"].startswith("golden:")
    capture = out.parent / "capture.jsonl"
    capture.write_text(
        "\n".join(
            json.dumps({"top_confidence": c, "refused": c < 0.3, "cached": False})
            for c in [0.9, 0.95, 0.1, 0.8, 0.85]
        )
    )
    from_traffic = build(
        corpus_dir,
        out.parent / "index2",
        embeddings=HashEmbeddings(),
        reranker=OverlapReranker(),
        golden=golden_path_of(out),
        baseline_run=None,
        capture=capture,
    )["baseline"]
    assert from_traffic["source"].startswith("capture:") and from_traffic["n"] == 5
    assert from_traffic["refusal_rate"] == pytest.approx(0.2)

    assert check(corpus_dir, out) == 0 and "index fresh" in capsys.readouterr().out
    (corpus_dir / "sla-2025.md").write_text((corpus_dir / "sla-2025.md").read_text() + "\n")
    try:
        assert is_stale(manifest, corpus_dir) is True
        assert check(corpus_dir, out) == 1 and "index stale" in capsys.readouterr().out
    finally:
        text = (corpus_dir / "sla-2025.md").read_text()
        (corpus_dir / "sla-2025.md").write_text(text[:-1])
    assert is_stale(manifest, corpus_dir) is False
    assert is_stale(manifest, corpus_dir / "missing") is None and is_stale(None, corpus_dir) is True


# ----- the free harness ---------------------------------------------------------------
async def test_retrieval_only_needs_no_model_and_gates_on_retrieval_alone(built, golden):
    out, _ = built
    index = PolicyIndex.load(
        out, HashEmbeddings(), out / "chunks.jsonl", reranker=OverlapReranker()
    )
    good = index.retrieve("Enterprise uptime", k=3)[0].chunk.id
    cases = [
        EvalCase(id="1", question="Enterprise uptime?", gold_chunk_ids=[good]),
        EvalCase(id="2", question="What colour is the office?", must_refuse=True),
    ]
    results, agg = await evaluate(None, index, cases, k=3, retrieval_only=True)
    assert agg["recall_at_k"] == 1.0 and agg["cost_usd_total"] == 0.0
    assert all(r.answer == "" and r.faithfulness is None for r in results)
    assert results[0].prompt_version == ANSWER_PROMPT.version
    # The refusal proxy is wrong here on purpose: gating on it would fail a healthy index.
    assert gate(agg, dict(agg, refusal_correct=1.0), keys=RETRIEVAL_KEYS) == []
    assert gate(dict(agg, mrr=agg["mrr"] - 0.5), agg, keys=RETRIEVAL_KEYS)
    assert GATE_KEYS[:2] == RETRIEVAL_KEYS


async def test_cli_report_records_versions_corpus_and_mode(
    built, golden, corpus_dir, tmp_path, monkeypatch
):
    out, manifest = built
    monkeypatch.setattr(evaluate_mod, "PolicyIndex", PolicyIndex)
    import nw.policy.retrieval as retrieval

    monkeypatch.setattr(retrieval, "real_embeddings", HashEmbeddings)
    monkeypatch.setattr(retrieval, "CrossEncoderReranker", OverlapReranker)
    report_path = tmp_path / "eval.json"
    baseline_path = tmp_path / "baseline.json"
    import argparse

    args = argparse.Namespace(
        golden=golden,
        index=out,
        chunks=out / "chunks.jsonl",
        baseline=baseline_path,
        corpus=corpus_dir,
        out=report_path,
        k=3,
        min_score=0.0,
        hybrid=True,
        rerank=True,
        judge=False,
        retrieval_only=True,
    )
    assert await evaluate_mod.main_async(args) == 0  # no baseline yet: nothing to gate
    report = json.loads(report_path.read_text())
    assert report["mode"] == "retrieval_only"
    assert report["prompt_versions"]["policy.answer"] == ANSWER_PROMPT.hash
    assert report["corpus_sha256_12"] == manifest["corpus_sha256_12"]
    assert report["golden_sha256_12"] == evaluate_mod.golden_sha(golden)
    assert (
        report["index_manifest"]["chunks"] == manifest["chunks"]
        and "baseline" not in report["index_manifest"]
    )
    baseline_path.write_text(report_path.read_text())
    assert await evaluate_mod.main_async(args) == 0
    stale = json.loads(baseline_path.read_text())
    stale["corpus_sha256_12"] = "000000000000"
    stale["aggregate"]["mrr"] = 1.5
    baseline_path.write_text(json.dumps(stale))
    assert await evaluate_mod.main_async(args) == 1


def test_workflow_runs_the_free_gate_on_pull_requests_and_the_judge_on_demand():
    text = WORKFLOW.read_text()
    assert "pull_request:" in text and "data/policies/**" in text and "nw/policy/**" in text
    assert "--retrieval-only" in text and "build_index --check" in text
    assert "workflow_dispatch" in text and "--no-judge" in text


# ----- the cache ----------------------------------------------------------------------
def test_cache_is_exact_match_ttl_and_size_bounded():
    assert normalise("  What is the SLA?? ") == normalise("what is the sla")
    assert normalise("What is the SLA for Pro?") != normalise("What is the SLA for Enterprise?")
    now = [100.0]
    c: ResponseCache[str] = ResponseCache(ttl_s=10, size=2, clock=lambda: now[0])
    k = ResponseCache.key("What is the SLA?", "customer", 8, "abc", "policy.answer@1", "m")
    assert k != ResponseCache.key("What is the SLA?", "customer", 8, "abc", "policy.answer@2", "m")
    assert k != ResponseCache.key("What is the SLA?", "customer", 8, "xyz", "policy.answer@1", "m")
    assert c.get(k) is None
    c.put(k, "answer")
    assert c.get(k) == "answer" and c.stats()["hits"] == 1 and c.stats()["misses"] == 1
    now[0] += 11
    assert c.get(k) is None  # expired
    c.put("a", "1"), c.put("b", "2"), c.put("c", "3")
    assert len(c) == 2 and c.get("a") is None and c.get("c") == "3"
    off: ResponseCache[str] = ResponseCache(ttl_s=0)
    off.put("x", "y")
    assert not off.enabled and off.get("x") is None and len(off) == 0


# ----- drift --------------------------------------------------------------------------
def test_monitor_warms_up_then_alerts_when_questions_leave_the_corpus():
    baseline = make_baseline(
        [0.85, 0.9, 0.8, 0.95, 0.7, 0.88], refusal_rate=0.2, answer_lengths=[80, 120, 200, 90, 150]
    )
    assert psi(baseline["confidence_hist"], baseline["confidence_hist"]) == pytest.approx(0.0)
    m = PolicyDriftMonitor(baseline, window=100, min_window=10)
    assert m.snapshot().level == "warming_up"
    for i in range(24):  # questions that look like the golden set
        m.observe([0.85, 0.9, 0.8, 0.95, 0.7, 0.88][i % 6], False, [80, 120, 200, 90, 150][i % 5])
    ok = m.snapshot()
    assert ok.level == "ok" and ok.confidence_psi is not None and ok.answer_length_psi is not None
    assert ok.refusal_rate == 0.0 and ok.baseline_refusal_rate == 0.2
    for _ in range(60):
        m.observe(0.12, True, 0)  # off-topic questions, every one refused
    alert = m.snapshot()
    assert alert.level == "alert" and alert.confidence_psi > 0.2 and alert.refusal_rate > 0.4
    refusals_only = PolicyDriftMonitor(baseline, window=100, min_window=10)
    for i in range(24):  # same confidences, refusals at 0.5 against a baseline of 0.2
        refusals_only.observe([0.85, 0.9, 0.8, 0.95, 0.7, 0.88][i % 6], i % 2 == 0, 120)
    assert refusals_only.snapshot().level == "alert"
    assert PolicyDriftMonitor(None).enabled is False


# ----- feedback -----------------------------------------------------------------------
def test_feedback_becomes_golden_candidates():
    records = [
        {
            "verdict": "wrong",
            "question": "What is the Pro uptime?",
            "citations": ["sla-2025#a"],
            "refused": False,
            "text": "99.5",
        },
        {
            "verdict": "unsafe",
            "question": "Duty manager rule?",
            "citations": ["escalation#b"],
            "refused": False,
        },
        {
            "verdict": "helpful",
            "question": "Refund window?",
            "citations": ["refunds#c"],
            "refused": False,
            "text": "30 days",
        },
        {
            "verdict": "wrong",
            "question": "what is the pro uptime",
            "citations": ["sla-2025#d"],
            "refused": True,
        },
    ]
    rows = to_golden(records)
    assert len(rows) == 2 and all(r["id"].startswith("fb-") for r in rows)
    by_q = {r["question"]: r for r in rows}
    assert by_q["what is the pro uptime"]["gold_chunk_ids"] == ["sla-2025#d"]  # newest verdict wins
    assert (
        by_q["Duty manager rule?"]["must_refuse"] is True
        and by_q["what is the pro uptime"]["must_refuse"] is False
    )
    assert all(r["gold_answer"] == "" for r in rows)
    everything = to_golden(records, include_helpful=True)
    assert (
        len(everything) == 3
        and next(r for r in everything if r["question"] == "Refund window?")["gold_answer"]
        == "30 days"
    )
    assert must_refuse_guess({"verdict": "unsafe"}) and not must_refuse_guess({"verdict": "wrong"})


# ----- the service --------------------------------------------------------------------
@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture
def served(built, corpus_dir, make_client, tmp_path, monkeypatch):
    out, _ = built
    index = PolicyIndex.load(
        out, HashEmbeddings(), out / "chunks.jsonl", reranker=OverlapReranker()
    )
    provider = FakeProvider(cite_first)
    monkeypatch.setenv("NW_POLICY_CORPUS", str(corpus_dir))
    monkeypatch.setenv("NW_POLICY_CACHE_TTL_S", "60")
    monkeypatch.setenv("NW_POLICY_CACHE_SIZE", "10")
    monkeypatch.setenv("NW_POLICY_FEEDBACK", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("NW_POLICY_CAPTURE", str(tmp_path / "capture.jsonl"))
    monkeypatch.setenv("NW_POLICY_DRIFT_MIN", "4")
    monkeypatch.setenv("NW_POLICY_DRIFT_EVERY", "4")
    monkeypatch.setattr(service, "state", service.State())
    service.state.index, service.state.client, service.state.ready = (
        index,
        make_client(provider),
        True,
    )
    service._configure_llmops()
    app = service.app
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c, provider, tmp_path


def test_ask_carries_ids_and_versions_and_the_cache_serves_the_repeat(served):
    c, provider, _ = served
    r1 = c.post("/ask", json={"question": "What uptime does Enterprise get?"}).json()
    assert not r1["refused"] and r1["cached"] is False and len(r1["answer_id"]) == 32
    assert r1["prompt_version"] == ANSWER_PROMPT.version and r1["model_id"] == "fake-workhorse"
    r2 = c.post("/ask", json={"question": "what uptime does enterprise get"}).json()
    assert r2["cached"] is True and r2["text"] == r1["text"] and r2["answer_id"] != r1["answer_id"]
    assert len(provider.calls) == 1, "the repeat never reached the model"
    r3 = c.post(
        "/ask", json={"question": "What uptime does Enterprise get?", "audience": "internal"}
    ).json()
    assert r3["cached"] is False and len(provider.calls) == 2, "audience is part of the key"
    text = c.get("/metrics").text
    assert (
        'nw_policy_cache_total{result="hit"}' in text
        and 'nw_policy_cache_total{result="miss"}' in text
    )
    assert "nw_policy_index_stale 0.0" in text


def test_version_reports_the_manifest_and_staleness(served, corpus_dir):
    c, _, _ = served
    v = c.get("/version").json()
    assert v["index_stale"] is False and v["manifest"]["corpus_sha256_12"] == corpus_sha(corpus_dir)
    assert "baseline" not in v["manifest"] and v["drift_baseline"] is True
    assert v["prompt_versions"]["policy.answer"] == ANSWER_PROMPT.hash
    assert v["prompts_changed_since_build"] == [] and v["cache"]["enabled"] is True
    (corpus_dir / "new-policy.md").write_text(
        "---\ntitle: New\ndoc_id: new\naudience: customer\neffective: 2026-01-01\nsupersedes: none\n---\n# New\n\n## Rule\n\nA new rule.\n"
    )
    try:
        service._configure_llmops()
        assert c.get("/version").json()["index_stale"] is True
        assert "nw_policy_index_stale 1.0" in c.get("/metrics").text
    finally:
        (corpus_dir / "new-policy.md").unlink()


def test_feedback_is_recorded_with_the_answer_and_bad_ids_are_rejected(served):
    c, _, tmp = served
    a = c.post("/ask", json={"question": "How are duplicate charges refunded?"}).json()
    r = c.post(
        "/feedback",
        json={
            "answer_id": a["answer_id"],
            "verdict": "wrong",
            "note": "says 5 days, policy says 10",
        },
    )
    assert r.status_code == 200 and r.json()["golden_candidate"] is True
    assert (
        c.post("/feedback", json={"answer_id": "0" * 32, "verdict": "helpful"}).status_code == 404
    )
    assert (
        c.post("/feedback", json={"answer_id": a["answer_id"], "verdict": "meh"}).status_code == 422
    )
    rows = load_feedback(tmp / "feedback.jsonl")
    assert len(rows) == 1 and rows[0]["question"] == "How are duplicate charges refunded?"
    assert (
        rows[0]["citations"] == a["citations"] and rows[0]["prompt_version"] == a["prompt_version"]
    )
    assert rows[0]["verdict"] == "wrong" and rows[0]["note"].startswith("says 5 days")
    candidates = to_golden(rows)
    assert (
        candidates[0]["gold_chunk_ids"] == a["citations"] and candidates[0]["must_refuse"] is False
    )
    assert 'nw_policy_feedback_total{verdict="wrong"}' in c.get("/metrics").text


def test_drift_and_capture_follow_every_uncached_ask(served):
    c, _, tmp = served
    assert c.get("/drift").json()["level"] == "warming_up"
    questions = [
        "What uptime does Enterprise get?",
        "How are duplicate charges refunded?",
        "P0 response time for Pro?",
        "Annual plan refunds?",
    ]
    for q in questions:
        assert c.post("/ask", json={"question": q}).status_code == 200
    snap = c.get("/drift").json()
    assert snap["window"] == 4 and snap["level"] in {"ok", "watch", "alert"}
    assert snap["confidence_psi"] is not None and snap["baseline_refusal_rate"] == pytest.approx(
        1 / 3
    )
    c.post("/ask", json={"question": questions[0]})  # a cache hit: captured, not observed
    assert c.get("/drift").json()["window"] == 4
    lines = [json.loads(line) for line in (tmp / "capture.jsonl").read_text().splitlines()]
    assert len(lines) == 5 and lines[-1]["cached"] is True and lines[0]["cached"] is False
    assert {
        "question",
        "citations",
        "confidence",
        "top_confidence",
        "refused",
        "prompt_version",
        "cached",
    } <= set(lines[0])
    text = c.get("/metrics").text
    assert (
        'nw_policy_drift_psi{feature="confidence"}' in text
        and "nw_policy_refusal_rate_window" in text
    )


# ----- helpers ------------------------------------------------------------------------
def chunk_corpus_of(corpus_dir):
    from nw.policy.chunking import chunk_corpus

    return chunk_corpus(corpus_dir)


def golden_path_of(out):
    return out.parent / "golden.jsonl"


def load_chunks_of(out):
    from nw.policy.chunking import load_chunks

    return load_chunks(out / "chunks.jsonl")
