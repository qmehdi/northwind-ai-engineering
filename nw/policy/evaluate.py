"""The evaluation harness for the policy service, and the regression gate.

Retrieval and generation are measured separately, because a bad answer from
good retrieval is a different bug from a bad answer from bad retrieval:

- retrieval: recall@k and MRR against the gold chunk ids
- generation: faithfulness judged by the Judge model against a rubric,
  citation validity computed deterministically, refusal correctness
- cost and latency per case, from the client's meter

    uv run python -m nw.policy.evaluate --golden data/golden/policy_qa.jsonl \
        --baseline data/golden/baseline.json
    uv run python -m nw.policy.evaluate --no-judge          # Workhorse only, about 1 USD
    uv run python -m nw.policy.evaluate --retrieval-only    # no model at all, free: CI

Every row and the report carry the prompt versions, the corpus hash, the golden set hash
and the index manifest, so a moved number can be traced to the prompt, the corpus, the
cases or the model that moved it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nw.config import ModelRole
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


class EvalCase(BaseModel):
    id: str
    question: str
    gold_answer: str = ""
    gold_chunk_ids: list[str] = Field(default_factory=list)
    must_refuse: bool = False
    audience: str = "customer"


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
    return 0.0, 0.0  # Step 6: recall@k and MRR against the gold chunk ids


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
    before = client.spend_usd if client else 0.0
    t0 = time.perf_counter()
    retrieved = index.retrieve(case.question, k=k, audience=case.audience, **retrieve_kw)
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
        )
    a = await answer(client, case.question, retrieved, min_score=min_score)
    latency = (time.perf_counter() - t0) * 1000
    recall, mrr = retrieval_metrics(retrieved, case.gold_chunk_ids)
    faith = await judge(client, case.question, a, index) if use_judge else None
    return CaseResult(
        id=case.id,
        recall_at_k=recall,
        mrr=mrr,
        faithfulness=faith,
        citation_validity=citation_validity(a),
        refused=a.refused,
        refusal_correct=(a.refused == case.must_refuse),
        latency_ms=latency,
        cost_usd=client.spend_usd - before,
        answer=a.text,
        citations=a.citations,
        prompt_version=a.prompt_version,
    )


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    if not results:
        raise ValueError("no cases: the golden set is empty or the filter removed everything")
    faith = [r.faithfulness for r in results if r.faithfulness is not None]
    lat = [r.latency_ms for r in results]
    return {
        "n": len(results),
        "recall_at_k": statistics.mean(r.recall_at_k for r in results),
        "mrr": statistics.mean(r.mrr for r in results),
        "faithfulness": statistics.mean(faith) if faith else None,
        "faithfulness_n": len(faith),
        "citation_validity": statistics.mean(r.citation_validity for r in results),
        "refusal_correct": statistics.mean(1.0 if r.refusal_correct else 0.0 for r in results),
        "must_refuse_caught": sum(1 for r in results if r.refused and r.refusal_correct),
        "latency_p50_ms": statistics.median(lat),
        "latency_p95_ms": sorted(lat)[int(0.95 * (len(lat) - 1))],
        "cost_usd_total": sum(r.cost_usd for r in results),
        "cost_usd_per_case": statistics.mean(r.cost_usd for r in results),
    }


def format_report(agg: dict[str, Any], label: str = "run") -> str:
    def f(v: Any) -> str:
        return "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))

    rows = [
        ("Cases", agg["n"]),
        ("Recall@k", agg["recall_at_k"]),
        ("MRR", agg["mrr"]),
        (f"Faithfulness (judged, n={agg['faithfulness_n']})", agg["faithfulness"]),
        ("Citation validity", agg["citation_validity"]),
        ("Refusal correct", agg["refusal_correct"]),
        ("p50 latency ms", agg["latency_p50_ms"]),
        ("p95 latency ms", agg["latency_p95_ms"]),
        ("Cost USD per case", agg["cost_usd_per_case"]),
    ]
    out = [f"| Metric | {label} |", "| --- | ---: |"]
    out += [f"| {k} | {f(v)} |" for k, v in rows]
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
    return []  # Step 7: fail when a metric drops more than max_drop below baseline


def comparability(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Warnings, not failures: the baseline was measured on a different corpus or with
    different prompts, so a moved number may be the corpus or the prompt, not a regression.
    A baseline written before these fields existed compares silently."""
    notes = []
    b_corpus, c_corpus = baseline.get("corpus_sha256_12"), current.get("corpus_sha256_12")
    if b_corpus and c_corpus and b_corpus != c_corpus:
        notes.append(
            f"baseline was measured on corpus {b_corpus}, this run on {c_corpus}: "
            "retrieval numbers are not comparable, regenerate the baseline"
        )
    b_golden, c_golden = baseline.get("golden_sha256_12"), current.get("golden_sha256_12")
    if b_golden and c_golden and b_golden != c_golden:
        notes.append(
            f"baseline was measured on golden set {b_golden}, this run on {c_golden}: "
            "the cases moved, so the numbers are not comparable, regenerate the baseline"
        )
    b_prompts, c_prompts = baseline.get("prompt_versions") or {}, current.get("prompt_versions")
    for name, h in (c_prompts or {}).items():
        if name in b_prompts and b_prompts[name] != h:
            notes.append(f"prompt {name} changed since the baseline: {b_prompts[name]} -> {h}")
    return notes


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


async def main_async(args: argparse.Namespace) -> int:
    from nw.config import settings
    from nw.llm.providers import make_provider
    from nw.policy.manifest import corpus_sha
    from nw.policy.retrieval import CrossEncoderReranker, real_embeddings

    s = settings()
    client = None if args.retrieval_only else LLMClient(make_provider(s), settings=s)
    index = PolicyIndex.load(
        args.index,
        real_embeddings(),
        args.chunks,
        reranker=CrossEncoderReranker() if args.rerank else None,
    )
    cases = load_cases(args.golden)
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
    report = {
        "aggregate": agg,
        "mode": "retrieval_only"
        if args.retrieval_only
        else ("judged" if args.judge else "no_judge"),
        "prompt_versions": prompts.versions(),
        "corpus_sha256_12": corpus_sha(args.corpus) if args.corpus.exists() else None,
        "golden_sha256_12": golden_sha(args.golden),
        "index_manifest": {k: v for k, v in index.manifest.items() if k != "baseline"},
        "models": {
            "workhorse": s.model_for(ModelRole.WORKHORSE),
            "judge": s.model_for(ModelRole.JUDGE) if args.judge else None,
        },
        "results": [r.model_dump() for r in results],
        "config": vars(args)
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
    print(format_report(agg, label=report["mode"]))
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text())
        for note in comparability(report, baseline):
            print(f"\nWARNING {note}")
        keys = RETRIEVAL_KEYS if args.retrieval_only else GATE_KEYS
        failures = gate(agg, baseline["aggregate"], keys=keys)
        if failures:
            print("\nREGRESSION GATE FAILED\n  " + "\n  ".join(failures))
            return 1
        print(f"\nregression gate passed ({', '.join(keys)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, default=Path("data/golden/policy_qa.jsonl"))
    ap.add_argument("--index", type=Path, default=Path("artifacts/policy"))
    ap.add_argument("--chunks", type=Path, default=Path("artifacts/policy/chunks.jsonl"))
    ap.add_argument("--baseline", type=Path, default=Path("data/golden/baseline.json"))
    ap.add_argument("--corpus", type=Path, default=Path("data/policies"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/policy/eval.json"))
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
        help="no model call at all: recall and MRR, gated on those two. Free; what CI runs",
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
