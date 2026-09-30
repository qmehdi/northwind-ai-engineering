"""The evaluation harness for the policy service, and the regression gate.

Retrieval and generation are measured separately, because a bad answer from
good retrieval is a different bug from a bad answer from bad retrieval:

- retrieval: recall@1, recall@3 and recall@k and MRR against the gold chunk ids
- generation: key-fact correctness against `gold_answer` (deterministic: the numbers,
  durations and names the answer must carry, and stale facts from a superseded policy it
  must not), citation recall against the gold chunks, citation validity, refusal
  correctness, and faithfulness judged by the Judge model against a rubric
- cost and latency per case, from the client's meter
- every metric per slice: dev and gate split, language, must-refuse, internal, multi-hop,
  currency (a current policy that superseded an older one)

    uv run python -m nw.policy.evaluate --retrieval-only    # no model at all, free: CI
    uv run python -m nw.policy.evaluate --no-judge          # Workhorse only, about 0.05 USD
    uv run python -m nw.policy.evaluate                     # plus the Judge, about 0.35 USD
    uv run python -m nw.policy.evaluate --no-judge --write-baseline   # this track's baseline

The golden set is split. `dev` cases are for tuning (k, the refusal threshold, the prompt);
`gate` cases are held out and are what the gate reads (`--split gate`, the default). A
threshold tuned on the gate split is a threshold tuned on the exam.

The gate refuses a comparison it cannot trust. Every report carries its provenance: the
mode, the track, the model ids (Workhorse, Judge, embedder, reranker), the prompt versions,
the corpus hash, the golden set hash and the split. A baseline without that provenance, a
baseline marked `legacy` (the Claude-era `data/golden/baseline.json`), or one measured with
other models, another corpus, other cases or another Judge prompt fails the gate with a
reason, instead of comparing silently. A deliberate model migration passes
`--allow-model-change`, which the decision records.

The prices are from `deploy/COSTS-platform.md` on gpt-oss-120b and Claude Opus 5, measured
prompt sizes: 66 answers at about 1,800 tokens in (0.05 USD on AWS and Azure, 0.03 on Google
Cloud) and about 44 judgements at about 450 in (0.0064 to 0.0070 USD each). A judged run gates
faithfulness only against a calibration of the same Judge id: your own report, or one committed
to `data/golden/calibrations/` (`nw.policy.calibrate --share`), so a cohort calibrates once.

Baselines live per mode and per track, because a number measured on one Workhorse says
nothing about another: `data/golden/baselines/policy-retrieval_only.json` (no model, one for
every track) and `data/golden/baselines/policy-<track>-<mode>.json`. `--write-baseline`
writes this run's report there when no baseline exists or the gate passed.

Small samples are stated. Rates carry their counts and a 95 percent Wilson interval; the
gate counts flipped cases for pass or fail metrics (refusal correctness, full recall, recall
at 1: `--max-case-flips`, default one case) and compares means for scores, with a paired
bootstrap interval of the difference printed as evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nw.config import ModelRole
from nw.evalstats import fmt_rate, paired_bootstrap, paired_counts, proportion
from nw.llm import LLMClient, prompts
from nw.llm.prompts import register
from nw.policy.answer import ANSWER_PROMPT, Answer, answer, weak_retrieval
from nw.policy.retrieval import PolicyIndex, Retrieved

GATE_KEYS: tuple[str, ...] = (
    "recall_at_k",
    "mrr",
    "faithfulness",
    "citation_validity",
    "refusal_correct",
)
RETRIEVAL_KEYS: tuple[str, ...] = ("recall_at_k", "mrr")
MODES = ("retrieval_only", "no_judge", "judged")
BASELINES = Path("data/golden/baselines")
LEGACY_BASELINE = Path("data/golden/baseline.json")
GATE_LOG = Path("artifacts/policy/eval_gate.jsonl")
CALIBRATION = Path("artifacts/policy/judge_calibration.json")
SHARED_CALIBRATIONS = Path("data/golden/calibrations")  # committed, one per Judge id
# Per mode: scores compared as means (the learner's `gate`), and pass or fail metrics
# compared as counts of flipped cases. A per-case pass is recall 1.0, a hit at rank 1, a
# correct refusal.
MEAN_KEYS: dict[str, tuple[str, ...]] = {
    "retrieval_only": ("mrr",),
    "no_judge": ("mrr", "key_fact_correctness", "citation_recall", "citation_validity"),
    "judged": (
        "mrr",
        "key_fact_correctness",
        "citation_recall",
        "citation_validity",
        "faithfulness",
    ),
}
COUNT_KEYS: dict[str, tuple[str, ...]] = {
    "retrieval_only": ("recall_at_k", "recall_at_1"),
    "no_judge": ("recall_at_k", "recall_at_1", "refusal_correct"),
    "judged": ("recall_at_k", "recall_at_1", "refusal_correct"),
}


class EvalCase(BaseModel):
    id: str
    question: str
    gold_answer: str = ""
    gold_chunk_ids: list[str] = Field(default_factory=list)
    must_refuse: bool = False
    audience: str = "customer"
    split: str = "gate"  # dev (tune on it) or gate (held out, what the gate reads)
    language: str = "en"
    slices: list[str] = Field(default_factory=list)  # multi_hop, currency, internal, ...
    # What a correct answer must carry. Each item is a fact or a list of equivalent spellings
    # ("30 days", "30 Tage"). None derives them from `gold_answer`.
    key_facts: list[str | list[str]] | None = None
    stale_facts: list[str] = Field(default_factory=list)  # from a superseded policy


class CaseResult(BaseModel):
    id: str
    recall_at_k: float
    mrr: float
    faithfulness: float | None
    citation_validity: float
    refused: bool
    refusal_correct: bool
    latency_ms: float
    cost_usd: float
    answer: str
    citations: list[str]
    prompt_version: str = ""
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    key_fact_correctness: float | None = None
    citation_recall: float | None = None
    stale_fact: bool = False
    top_confidence: float | None = None
    split: str = "gate"
    language: str = "en"
    slices: list[str] = Field(default_factory=list)
    must_refuse: bool = False


class Judgement(BaseModel):
    faithfulness: float = Field(ge=0, le=1)
    reason: str


JUDGE_SYSTEM = """You grade whether an answer is faithful to the context passages it cites.
Score 1.0 when every claim in the answer is supported by the passages, 0.5 when the answer is
mostly supported but adds or alters a detail, 0.0 when it contradicts the passages or answers
from outside them. Judge support, not style. Reply with the score and one sentence of reason."""

JUDGE_PROMPT = register("policy.judge", JUDGE_SYSTEM)


def retrieval_metrics(retrieved: list[Retrieved], gold: list[str]) -> tuple[float, float]:
    """recall@k: share of gold chunks in the retrieved list. MRR: 1 / rank of the first gold hit."""
    # SOLUTION BEGIN
    if not gold:
        return 1.0, 1.0
    ids = [r.chunk.id for r in retrieved]
    hits = sum(1 for g in gold if g in ids)
    rr = 0.0
    for rank, cid in enumerate(ids, 1):
        if cid in gold:
            rr = 1.0 / rank
            break
    return hits / len(gold), rr
    # STUB: return 0.0, 0.0  # the harness step: recall@k and MRR against the gold ids
    # SOLUTION END


def recall_at(retrieved: list[Retrieved], gold: list[str], n: int) -> float:
    """Share of the gold chunks in the top n."""
    if not gold:
        return 1.0
    ids = [r.chunk.id for r in retrieved[:n]]
    return sum(1 for g in gold if g in ids) / len(gold)


_NUMBER_FACT = re.compile(
    r"(?:\d[\d,.]*\d|\d)"
    r"(?:\s*(?:percent|%|business days?|days?|hours?|minutes?|months?|years?|weeks?|gb|tb|usd|"
    r"requests per minute|attempts?|codes?))?",
    re.IGNORECASE,
)
_UNIT_PLURAL = re.compile(r"\b(day|hour|minute|month|year|week|attempt|code)s\b")


def normalise(text: str) -> str:
    """Lower case, hyphens as spaces, `%` as percent, singular units, one space."""
    t = text.lower().replace("-", " ").replace("%", " percent")
    t = _UNIT_PLURAL.sub(r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def derive_key_facts(gold_answer: str) -> list[str]:
    """The checkable facts in a gold answer: every number with its unit. A gold answer with
    no number yields none, and correctness is not scored for that case."""
    facts = []
    for m in _NUMBER_FACT.finditer(gold_answer):
        fact = m.group(0).strip().rstrip(".,")
        if fact and fact not in facts:
            facts.append(fact)
    return facts


def key_fact_correctness(case: EvalCase, answer: str) -> tuple[float | None, bool]:
    """(share of key facts the answer carries, whether it carries a stale fact)."""
    facts = case.key_facts if case.key_facts is not None else derive_key_facts(case.gold_answer)
    text = normalise(answer)
    stale = any(normalise(s) in text for s in case.stale_facts)
    if not facts:
        return None, stale
    found = 0
    for fact in facts:
        options = [fact] if isinstance(fact, str) else fact
        if any(normalise(o) in text for o in options):
            found += 1
    return found / len(facts), stale


def citation_recall(a: Answer, gold: list[str]) -> float | None:
    """Share of the gold chunks the answer cited. None when nothing is expected."""
    if a.refused or not gold:
        return None
    return sum(1 for g in gold if g in a.citations) / len(gold)


def citation_validity(a: Answer) -> float:
    """Deterministic: share of the model's citations that were in the context. An answer that
    was refused has nothing to validate and scores 1."""
    if a.refused:
        return 1.0
    total = len(a.citations) + len(a.dropped_citations)
    return 1.0 if total == 0 else len(a.citations) / total


async def judge(client: LLMClient, question: str, a: Answer, index: PolicyIndex) -> float | None:
    if a.refused:
        return None
    passages = "\n\n".join(
        f"[{cid}]\n{index.by_id[cid].text}" for cid in a.citations if cid in index.by_id
    )
    prompt = f"Question: {question}\n\nAnswer: {a.text}\n\nCited passages:\n{passages}"
    j = await client.structured(
        prompt, Judgement, role=ModelRole.JUDGE, system=JUDGE_PROMPT.text, max_tokens=300
    )
    return j.faithfulness


def _labels(case: EvalCase) -> dict[str, Any]:
    return {
        "split": case.split,
        "language": case.language,
        "slices": case.slices,
        "must_refuse": case.must_refuse,
    }


async def run_case(
    client: LLMClient | None,
    index: PolicyIndex,
    case: EvalCase,
    *,
    k: int,
    min_score: float,
    use_judge: bool = True,
    retrieval_only: bool = False,
    **retrieve_kw: Any,
) -> CaseResult:
    """One case. `retrieval_only` never touches a model: recall and MRR are real, the refusal
    is the weak-retrieval rule alone, and the answer is empty. That is the free mode CI runs."""
    t0 = time.perf_counter()
    retrieved = index.retrieve(case.question, k=k, audience=case.audience, **retrieve_kw)
    ranks = {
        "recall_at_1": recall_at(retrieved, case.gold_chunk_ids, 1),
        "recall_at_3": recall_at(retrieved, case.gold_chunk_ids, 3),
        "top_confidence": retrieved[0].confidence if retrieved else 0.0,
    }
    if retrieval_only or client is None:
        refused = weak_retrieval(retrieved, min_score)
        latency = (time.perf_counter() - t0) * 1000
        recall, mrr = retrieval_metrics(retrieved, case.gold_chunk_ids)
        return CaseResult(
            id=case.id,
            recall_at_k=recall,
            mrr=mrr,
            faithfulness=None,
            citation_validity=1.0,
            refused=refused,
            refusal_correct=(refused == case.must_refuse),
            latency_ms=latency,
            cost_usd=0.0,
            answer="",
            citations=[],
            prompt_version=ANSWER_PROMPT.version,
            **ranks,
            **_labels(case),
        )
    # One cost scope per case: cases run concurrently on one client, so the difference of
    # the client's total spend would charge a case for its neighbours' calls.
    with client.cost_scope() as scope:
        a = await answer(client, case.question, retrieved, min_score=min_score)
        latency = (time.perf_counter() - t0) * 1000
        recall, mrr = retrieval_metrics(retrieved, case.gold_chunk_ids)
        faith = await judge(client, case.question, a, index) if use_judge else None
    correct, stale = (None, False) if a.refused else key_fact_correctness(case, a.text)
    return CaseResult(
        id=case.id,
        recall_at_k=recall,
        mrr=mrr,
        faithfulness=faith,
        citation_validity=citation_validity(a),
        refused=a.refused,
        refusal_correct=(a.refused == case.must_refuse),
        latency_ms=latency,
        cost_usd=scope.total_usd,
        answer=a.text,
        citations=a.citations,
        prompt_version=a.prompt_version,
        key_fact_correctness=correct,
        citation_recall=citation_recall(a, case.gold_chunk_ids),
        stale_fact=stale,
        **ranks,
        **_labels(case),
    )


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    """Point numbers, the counts behind the pass or fail ones with Wilson intervals, and the
    same numbers per slice."""
    if not results:
        raise ValueError("no cases: the golden set is empty or the filter removed everything")
    faith = [r.faithfulness for r in results if r.faithfulness is not None]
    correct = [r.key_fact_correctness for r in results if r.key_fact_correctness is not None]
    cite_recall = [r.citation_recall for r in results if r.citation_recall is not None]
    lat = [r.latency_ms for r in results]
    answered = [r for r in results if not r.refused]
    agg = {
        "n": len(results),
        "recall_at_k": statistics.mean(r.recall_at_k for r in results),
        "recall_at_1": statistics.mean(r.recall_at_1 for r in results),
        "recall_at_3": statistics.mean(r.recall_at_3 for r in results),
        "mrr": statistics.mean(r.mrr for r in results),
        "faithfulness": _mean(faith),
        "faithfulness_n": len(faith),
        "key_fact_correctness": _mean(correct),
        "key_fact_n": len(correct),
        "citation_recall": _mean(cite_recall),
        "stale_facts": sum(1 for r in answered if r.stale_fact),
        "citation_validity": statistics.mean(r.citation_validity for r in results),
        "refusal_correct": statistics.mean(1.0 if r.refusal_correct else 0.0 for r in results),
        "must_refuse_caught": sum(1 for r in results if r.refused and r.refusal_correct),
        "latency_p50_ms": statistics.median(lat),
        "latency_p95_ms": sorted(lat)[int(0.95 * (len(lat) - 1))],
        "cost_usd_total": sum(r.cost_usd for r in results),
        "cost_usd_per_case": statistics.mean(r.cost_usd for r in results),
        "intervals": {
            "recall_full": proportion(
                sum(1 for r in results if r.recall_at_k >= 1.0), len(results)
            ),
            "recall_at_1_hit": proportion(
                sum(1 for r in results if r.recall_at_1 >= 1.0), len(results)
            ),
            "refusal_correct": proportion(
                sum(1 for r in results if r.refusal_correct), len(results)
            ),
            "must_refuse_caught": proportion(
                sum(1 for r in results if r.must_refuse and r.refused),
                sum(1 for r in results if r.must_refuse),
            ),
            "key_facts_complete": proportion(sum(1 for x in correct if x >= 1.0), len(correct)),
        },
    }
    agg["by_slice"] = by_slice(results)
    return agg


def slice_names(r: CaseResult) -> list[str]:
    names = [f"split:{r.split}", f"language:{r.language}"]
    names.append("must_refuse" if r.must_refuse else "answerable")
    names += [f"slice:{x}" for x in r.slices]
    return names


def by_slice(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    """Every slice with its size: a number on six cases is printed with n=6, not hidden."""
    groups: dict[str, list[CaseResult]] = {}
    for r in results:
        for name in slice_names(r):
            groups.setdefault(name, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for name, rows in sorted(groups.items()):
        correct = [r.key_fact_correctness for r in rows if r.key_fact_correctness is not None]
        faith = [r.faithfulness for r in rows if r.faithfulness is not None]
        out[name] = {
            "n": len(rows),
            "recall_at_k": statistics.mean(r.recall_at_k for r in rows),
            "recall_at_1": statistics.mean(r.recall_at_1 for r in rows),
            "refusal_correct": proportion(sum(1 for r in rows if r.refusal_correct), len(rows)),
            "key_fact_correctness": _mean(correct),
            "faithfulness": _mean(faith),
        }
    return out


def format_report(agg: dict[str, Any], label: str = "run") -> str:
    def f(v: Any) -> str:
        return "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))

    iv = agg.get("intervals") or {}
    rows = [
        ("Cases", agg["n"]),
        ("Recall@1", agg.get("recall_at_1")),
        ("Recall@3", agg.get("recall_at_3")),
        ("Recall@k", agg["recall_at_k"]),
        ("MRR", agg["mrr"]),
        (f"Key-fact correctness (n={agg.get('key_fact_n', 0)})", agg.get("key_fact_correctness")),
        ("Citation recall", agg.get("citation_recall")),
        (f"Faithfulness (judged, n={agg['faithfulness_n']})", agg["faithfulness"]),
        ("Citation validity", agg["citation_validity"]),
        ("Refusal correct", agg["refusal_correct"]),
        ("p50 latency ms", agg["latency_p50_ms"]),
        ("p95 latency ms", agg["latency_p95_ms"]),
        ("Cost USD per case", agg["cost_usd_per_case"]),
    ]
    out = [f"| Metric | {label} |", "| --- | ---: |"]
    out += [f"| {k} | {f(v)} |" for k, v in rows]
    if iv:
        out += ["", "Counts with 95% Wilson intervals:"]
        out += [f"- {name}: {fmt_rate(p)}" for name, p in iv.items() if p.get("n")]
    slices = agg.get("by_slice") or {}
    if slices:
        out += ["", "| Slice | n | Recall@k | Recall@1 | Refusal correct | Key facts |"]
        out += ["| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, r in slices.items():
            out.append(
                f"| {name} | {r['n']} | {r['recall_at_k']:.3f} | {r['recall_at_1']:.3f} | "
                f"{r['refusal_correct']['k']}/{r['refusal_correct']['n']} | "
                f"{f(r['key_fact_correctness'])} |"
            )
    return "\n".join(out)


def gate(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    max_drop: float = 0.03,
    keys: tuple[str, ...] = GATE_KEYS,
) -> list[str]:
    """Names of metrics that regressed beyond the tolerance. Empty means pass. `keys` narrows
    the comparison: the retrieval-only run in CI gates on recall and MRR alone, because its
    refusals and citations never met a model."""
    # SOLUTION BEGIN
    failures = []
    for key in keys:
        cur, base = current.get(key), baseline.get(key)
        if cur is None or base is None:
            continue
        if cur < base - max_drop:
            failures.append(f"{key}: {cur:.3f} < baseline {base:.3f} - {max_drop}")
    if "citation_validity" in keys and current["citation_validity"] < 1.0:
        failures.append(f"citation_validity must be 1.0, got {current['citation_validity']:.3f}")
    return failures
    # STUB: return []  # the regression gate step: fail on a drop past max_drop
    # SOLUTION END


def comparability(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Notes, not failures: what moved between the baseline and this run that the gate is
    meant to measure, a prompt version above all. What makes a comparison invalid (models,
    corpus, cases, the Judge's own prompt) is in `provenance_problems` and fails the gate."""
    notes = []
    b_prompts, c_prompts = baseline.get("prompt_versions") or {}, current.get("prompt_versions")
    for name, h in (c_prompts or {}).items():
        if name in b_prompts and b_prompts[name] != h:
            notes.append(f"prompt {name} changed since the baseline: {b_prompts[name]} -> {h}")
    b_cfg, c_cfg = baseline.get("config") or {}, current.get("config") or {}
    for key in ("k", "min_score", "hybrid", "rerank"):
        if key in b_cfg and key in c_cfg and b_cfg[key] != c_cfg[key]:
            notes.append(f"retrieval setting {key} changed: {b_cfg[key]} -> {c_cfg[key]}")
    return notes


PROVENANCE = ("mode", "golden_sha256_12", "corpus_sha256_12", "prompt_versions", "models")


def retrieval_models(report: dict[str, Any]) -> dict[str, Any]:
    m = report.get("index_manifest") or {}
    return {"embedder": m.get("embedder"), "reranker": m.get("reranker")}


def provenance_problems(
    current: dict[str, Any], baseline: dict[str, Any], *, allow_model_change: bool = False
) -> tuple[list[str], list[str]]:
    """(failures, notes). A baseline the gate cannot compare with is a failure with a reason:
    legacy, missing provenance, another mode, split, corpus or case set, other models, or
    another Judge prompt. `allow_model_change` turns a model difference into a note, for a
    deliberate migration measured against the old model's baseline."""
    fails: list[str] = []
    notes: list[str] = []
    if baseline.get("legacy"):
        fails.append(
            "the baseline is marked legacy: " + str(baseline.get("legacy_reason", "no provenance"))
        )
        return fails, notes
    missing = [k for k in PROVENANCE if baseline.get(k) in (None, {}, "")]
    if missing:
        fails.append(
            f"the baseline has no {', '.join(missing)}: it cannot be compared; write a new one "
            "with --write-baseline"
        )
        return fails, notes
    mode = current.get("mode")
    for key in ("mode", "split", "golden_sha256_12", "corpus_sha256_12"):
        b, c = baseline.get(key), current.get(key)
        if b != c:
            fails.append(f"{key} differs: baseline {b}, this run {c}; the numbers do not compare")
    if mode != "retrieval_only" and baseline.get("track") != current.get("track"):
        fails.append(
            f"track differs: baseline {baseline.get('track')}, this run {current.get('track')}"
        )
    b_models = {**(baseline.get("models") or {}), **retrieval_models(baseline)}
    c_models = {**(current.get("models") or {}), **retrieval_models(current)}
    roles = ["embedder", "reranker"]
    if mode != "retrieval_only":
        roles.append("workhorse")
    if mode == "judged":
        roles.append("judge")
    for role in roles:
        if b_models.get(role) != c_models.get(role):
            text = (
                f"{role} model differs: baseline {b_models.get(role)}, "
                f"this run {c_models.get(role)}"
            )
            if allow_model_change and role != "judge":
                notes.append(f"{text} (allowed: a deliberate model change)")
            else:
                fails.append(text)
    if mode == "judged":
        b_j = (baseline.get("prompt_versions") or {}).get("policy.judge")
        c_j = (current.get("prompt_versions") or {}).get("policy.judge")
        if b_j != c_j:
            fails.append(f"the Judge prompt differs ({b_j} -> {c_j}): the instrument changed")
    return fails, notes


@dataclass
class EvalPolicy:
    max_drop: float = 0.03  # mean scores against the baseline
    max_case_flips: int = 1  # pass or fail metrics: net cases lost against the baseline
    allow_model_change: bool = False
    require_calibrated_judge: bool = False


@dataclass
class EvalDecision:
    passed: bool
    mode: str
    baseline: str | None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    decided_at: str = ""


def _per_case(results: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    return {r["id"]: r.get(key) for r in results}


def _passes(key: str, value: Any) -> bool:
    if key == "refusal_correct":
        return bool(value)
    return (value or 0.0) >= 1.0


def decide(
    report: dict[str, Any],
    baseline: dict[str, Any] | None,
    policy: EvalPolicy | None = None,
    baseline_path: str | None = None,
) -> EvalDecision:
    """The gate: provenance first, then means through `gate`, then flipped cases counted and
    paired on the same case ids."""
    policy = policy or EvalPolicy()
    mode = report["mode"]
    d = EvalDecision(
        passed=True,
        mode=mode,
        baseline=baseline_path,
        models={**(report.get("models") or {}), **retrieval_models(report)},
        decided_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    if baseline is None:
        d.notes.append("no baseline for this track and mode: measured, not gated")
        return d
    fails, notes = provenance_problems(
        report, baseline, allow_model_change=policy.allow_model_change
    )
    d.notes += notes + comparability(report, baseline)
    if fails:
        d.passed, d.reasons = False, [f"[provenance] {x}" for x in fails]
        return d
    mean_keys = list(MEAN_KEYS[mode])
    cal = report.get("judge_calibration") or {}
    if mode == "judged" and not cal.get("matches_judge"):
        text = (
            f"the Judge {d.models.get('judge')} has no calibration report for this model id "
            f"({CALIBRATION}): faithfulness is reported, not gated"
        )
        if policy.require_calibrated_judge:
            d.reasons.append(f"[judge] {text}")
        else:
            d.notes.append(text)
            mean_keys.remove("faithfulness")
    d.reasons += gate(
        report["aggregate"], baseline["aggregate"], max_drop=policy.max_drop, keys=tuple(mean_keys)
    )
    cur, base = report.get("results") or [], baseline.get("results") or []
    ids = [r["id"] for r in cur]
    if sorted(ids) != sorted(r["id"] for r in base):
        d.reasons.append("[provenance] the case ids differ from the baseline's")
    else:
        for key in COUNT_KEYS[mode]:
            c_map, b_map = _per_case(cur, key), _per_case(base, key)
            if any(v is None for v in b_map.values()):
                d.notes.append(f"the baseline has no per-case {key}: not compared")
                continue
            pc = paired_counts(
                [_passes(key, b_map[i]) for i in ids], [_passes(key, c_map[i]) for i in ids]
            )
            d.evidence[key] = pc
            lost = pc["champion_only"] - pc["challenger_only"]
            if lost > policy.max_case_flips:
                d.reasons.append(
                    f"{key}: {pc['champion_only']} cases lost and {pc['challenger_only']} gained "
                    f"against the baseline (net {lost}, McNemar p={pc['mcnemar_p']:.3f}); the bar "
                    f"allows {policy.max_case_flips}"
                )
        for key in mean_keys:
            c_map, b_map = _per_case(cur, key), _per_case(base, key)
            both = [i for i in ids if c_map.get(i) is not None and b_map.get(i) is not None]
            if len(both) >= 5:
                pb = paired_bootstrap([b_map[i] for i in both], [c_map[i] for i in both])
                d.evidence[f"{key}_diff"] = pb
                d.notes.append(
                    f"{key} paired difference {pb['diff']:+.3f} over {pb['n']} cases, "
                    f"95% CI {pb['lo']:+.3f} to {pb['hi']:+.3f}"
                )
    d.passed = not d.reasons
    return d


def format_decision(d: EvalDecision) -> str:
    head = "REGRESSION GATE PASSED" if d.passed else "REGRESSION GATE FAILED"
    lines = [f"{head} ({d.mode}, against {d.baseline or 'no baseline'})"]
    lines += [f"  FAIL {r}" for r in d.reasons]
    lines += [f"  note {n}" for n in d.notes]
    return "\n".join(lines)


def baseline_path(track: str, mode: str, root: Path = BASELINES) -> Path:
    """Retrieval-only runs no model, so one baseline serves every track; the others are per
    track because the Workhorse and the Judge are."""
    if mode == "retrieval_only":
        return root / "policy-retrieval_only.json"
    return root / f"policy-{track}-{mode}.json"


def shared_calibration(
    judge_model: str | None, pattern: str, root: Path = SHARED_CALIBRATIONS
) -> Path | None:
    """A committed calibration report (`data/golden/calibrations/<pattern>`) that measured this
    Judge model id, or None. The Judge is the same model for every learner on a track, so one
    person (the instructor, in cohort mode) calibrates it once and commits the report; every
    other checkout reads it instead of paying for the same measurement again."""
    if not judge_model or not root.is_dir():
        return None
    for p in sorted(root.glob(pattern)):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("judge_model") == judge_model:
                return p
        except (OSError, ValueError):
            continue
    return None


def judge_calibration(
    judge_model: str | None, path: Path = CALIBRATION, shared: Path | None = None
) -> dict[str, Any]:
    """The calibration report on disk, and whether it measured this Judge. Your own report
    (`artifacts/policy/judge_calibration.json`) wins when it measured this Judge; otherwise a
    committed shared report for the same Judge id is used (`shared`, the default directory
    `data/golden/calibrations/` when `path` is the default)."""
    if shared is None and path == CALIBRATION:
        shared = SHARED_CALIBRATIONS
    own = path.exists() and json.loads(path.read_text(encoding="utf-8")).get("judge_model")
    if (not path.exists() or own != judge_model) and shared is not None:
        found = shared_calibration(judge_model, "policy-judge*.json", shared)
        if found is not None:
            path = found
    if not path.exists():
        return {"found": False, "matches_judge": False}
    rep = json.loads(path.read_text(encoding="utf-8"))
    return {
        "found": True,
        "path": str(path),
        "judge_model": rep.get("judge_model"),
        "matches_judge": bool(judge_model) and rep.get("judge_model") == judge_model,
        "n": rep.get("n"),
        "kappa": rep.get("kappa"),
        "false_pass_rate": rep.get("false_pass_rate"),
        "mean_bias": (rep.get("mean_score") or {}).get("bias"),
    }


def golden_sha(path: Path) -> str:
    """Twelve hex characters over the golden file's bytes: a case added, edited or removed
    changes it, and a baseline measured on other cases says so."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def load_cases(path: Path) -> list[EvalCase]:
    return [
        EvalCase.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


async def evaluate(
    client: LLMClient | None,
    index: PolicyIndex,
    cases: list[EvalCase],
    *,
    k: int = 8,
    min_score: float = 0.0,
    concurrency: int = 8,
    use_judge: bool = True,
    retrieval_only: bool = False,
    **retrieve_kw: Any,
) -> tuple[list[CaseResult], dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(c: EvalCase) -> CaseResult:
        async with sem:
            return await run_case(
                client,
                index,
                c,
                k=k,
                min_score=min_score,
                use_judge=use_judge,
                retrieval_only=retrieval_only,
                **retrieve_kw,
            )

    results = await asyncio.gather(*(one(c) for c in cases))
    return list(results), aggregate(results)


def select(cases: list[EvalCase], split: str) -> list[EvalCase]:
    """`gate` (held out, the default), `dev` (tuning) or `all`."""
    return cases if split == "all" else [c for c in cases if c.split == split]


async def main_async(args: argparse.Namespace) -> int:
    from nw.config import settings
    from nw.llm.providers import make_provider
    from nw.policy.manifest import corpus_sha
    from nw.policy.retrieval import CrossEncoderReranker, real_embeddings

    split = getattr(args, "split", "gate")
    s = settings()
    if args.judge and not args.retrieval_only and (why := s.judge_unavailable()):
        print(f"judged run refused: {why}")
        return 2
    client = None if args.retrieval_only else LLMClient(make_provider(s), settings=s)
    index = PolicyIndex.load(
        args.index,
        real_embeddings(),
        args.chunks,
        reranker=CrossEncoderReranker() if args.rerank else None,
    )
    cases = select(load_cases(args.golden), split)
    results, agg = await evaluate(
        client,
        index,
        cases,
        k=args.k,
        min_score=args.min_score,
        use_judge=args.judge,
        retrieval_only=args.retrieval_only,
        hybrid=args.hybrid,
        rerank=args.rerank,
    )
    prompts.load_known()
    mode = "retrieval_only" if args.retrieval_only else ("judged" if args.judge else "no_judge")
    judge_model = s.model_for(ModelRole.JUDGE) if mode == "judged" else None
    report = {
        "aggregate": agg,
        "mode": mode,
        "track": s.track.value,
        "split": split,
        "prompt_versions": prompts.versions(),
        "corpus_sha256_12": corpus_sha(args.corpus) if args.corpus.exists() else None,
        "golden_sha256_12": golden_sha(args.golden),
        "index_manifest": {k: v for k, v in index.manifest.items() if k != "baseline"},
        "models": {
            "workhorse": None if args.retrieval_only else s.model_for(ModelRole.WORKHORSE),
            "judge": judge_model,
        },
        "judge_calibration": judge_calibration(judge_model) if judge_model else None,
        "written_at": dt.datetime.now(dt.UTC).isoformat(),
        "results": [r.model_dump() for r in results],
        "config": {
            k: v
            for k, v in vars(args).items()
            if isinstance(v, str | int | float | bool | type(None))
        }
        | {
            "golden": str(args.golden),
            "index": str(args.index),
            "chunks": str(args.chunks),
            "out": str(args.out),
            "baseline": str(args.baseline),
            "corpus": str(args.corpus),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, default=str))
    print(format_report(agg, label=f"{mode}, {split} split"))
    path = resolve_baseline(args.baseline, s.track.value, mode)
    baseline = json.loads(path.read_text()) if path and path.exists() else None
    policy = EvalPolicy(
        max_drop=getattr(args, "max_drop", 0.03),
        max_case_flips=getattr(args, "max_case_flips", 1),
        allow_model_change=getattr(args, "allow_model_change", False),
        require_calibrated_judge=getattr(args, "require_calibrated_judge", False),
    )
    decision = decide(report, baseline, policy, str(path) if path else None)
    print()
    print(format_decision(decision))
    log = getattr(args, "gate_log", GATE_LOG)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(decision), default=str) + "\n")
    rebase = getattr(args, "rebase", None)
    if getattr(args, "write_baseline", False) and path is not None:
        if baseline is None or decision.passed or rebase:
            if baseline is not None and not decision.passed:
                # A deliberate rebase (a new golden set, corpus or model): recorded, never silent.
                report["rebased"] = {
                    "reason": rebase,
                    "previous": {k: baseline.get(k) for k in (*PROVENANCE, "split", "legacy")},
                    "gate_reasons": list(decision.reasons),
                }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=1, default=str) + "\n")
            print(f"baseline written to {path}" + (f" (rebased: {rebase})" if rebase else ""))
        else:
            print(
                f"baseline NOT written: the gate failed against {path}; after a deliberate "
                'change (new golden set, corpus or model) pass --rebase "<reason>"'
            )
    if baseline is None and getattr(args, "require_baseline", False):
        print(f"no baseline at {path}: --require-baseline makes that a failure")
        return 1
    return 0 if decision.passed else 1


def resolve_baseline(value: Any, track: str, mode: str) -> Path | None:
    """`auto` is this track and mode's file under data/golden/baselines; `none` turns the gate
    off; anything else is a path."""
    if value is None or str(value) == "none":
        return None
    if str(value) == "auto":
        return baseline_path(track, mode)
    return Path(value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, default=Path("data/golden/policy_qa.jsonl"))
    ap.add_argument("--index", type=Path, default=Path("artifacts/policy"))
    ap.add_argument("--chunks", type=Path, default=Path("artifacts/policy/chunks.jsonl"))
    ap.add_argument(
        "--baseline",
        default="auto",
        help="auto (data/golden/baselines/policy-<track>-<mode>.json), none, or a path",
    )
    ap.add_argument("--corpus", type=Path, default=Path("data/policies"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/policy/eval.json"))
    ap.add_argument("--split", choices=("gate", "dev", "all"), default="gate")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--no-hybrid", dest="hybrid", action="store_false")
    ap.add_argument("--no-rerank", dest="rerank", action="store_false")
    ap.add_argument(
        "--no-judge",
        dest="judge",
        action="store_false",
        help="skip faithfulness; the Workhorse still answers",
    )
    ap.add_argument(
        "--retrieval-only",
        action="store_true",
        help="no model call at all: recall and MRR, gated on those. Free; what CI runs",
    )
    ap.add_argument("--max-drop", type=float, default=0.03, help="mean scores against baseline")
    ap.add_argument("--max-case-flips", type=int, default=1, help="net cases lost, pass or fail")
    ap.add_argument("--allow-model-change", action="store_true")
    ap.add_argument("--require-calibrated-judge", action="store_true")
    ap.add_argument("--require-baseline", action="store_true", help="no baseline is a failure")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument(
        "--rebase",
        metavar="REASON",
        help="with --write-baseline: replace a baseline the gate refuses, recording why",
    )
    ap.add_argument("--gate-log", type=Path, default=GATE_LOG)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
