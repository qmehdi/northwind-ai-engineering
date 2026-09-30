"""Calibrate the Judge against human labels before trusting its faithfulness score.

A judge model is a measurement instrument. Like any instrument it needs calibrating: a set
of answers a person has labelled faithful or not, run through the same rubric the harness
uses, and a report of how often the two agree. The number that matters most is the false
pass rate, the share of unfaithful answers the judge waved through, because that is the
failure the regression gate cannot see.

Two things are calibrated, because the gate uses two things:

- the decision (a score at or above `--threshold` counts as faithful): agreement, Cohen's
  kappa, false pass and false fail rates, each with its count and a 95 percent interval
  (Wilson for rates, bootstrap for kappa). 0 false passes out of 14 is compatible with a
  true rate up to about 23 percent; the interval says so.
- the mean score, which is what the regression gate compares with `max_drop` 0.03: the
  judge's mean against the human mean (faithful 1, unfaithful 0, or the case's
  `human_score`), the bias with a bootstrap interval, and the mean absolute error. A bias
  interval wider than the gate's tolerance means the gate cannot see a drop that small.

The report records the Judge model id and the judge prompt version; the evaluation harness
reads it and gates faithfulness only when the calibration measured the Judge it is using.
The Judge is one model id for every learner on a track, so the report is shareable: `--share`
also writes `data/golden/calibrations/policy-judge-<track>.json`, which the harness reads
when a checkout has no report of its own for that Judge. 102 judgements at about 370 tokens
in cost about 0.61 USD at 5.00 and 25.00 per million, 0.67 at Bedrock's in-region price.

    uv run python -m nw.policy.calibrate                # Judge role on your track, about 0.65 USD
    uv run python -m nw.policy.calibrate --share        # and commit it for the cohort
    uv run python -m nw.policy.calibrate --threshold 0.5
    uv run python -m nw.policy.calibrate --from-eval artifacts/policy/eval.json \
        --candidates artifacts/policy/calibration_candidates.jsonl   # Workhorse answers to label

Cases live in `data/golden/judge_calibration.jsonl` (102: 30 written by hand, 72 built from
real passages with a known perturbation) and carry their own passage text, so they stay
valid when the chunker or the corpus changes. `--from-eval` turns the answers of a live run
into unlabelled cases, which is how the current Workhorse's own answers enter the set: a
person fills in `human_faithful` and appends them. Add a case whenever the judge surprises you.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nw.config import ModelRole
from nw.evalstats import bootstrap_ci, fmt_rate, proportion
from nw.llm import LLMClient
from nw.policy.evaluate import JUDGE_PROMPT, JUDGE_SYSTEM, SHARED_CALIBRATIONS, Judgement

CASES = Path("data/golden/judge_calibration.jsonl")
JudgeFn = Callable[[str, str, str], Awaitable[float]]


class CalibrationCase(BaseModel):
    id: str
    question: str
    answer: str
    passage: str
    human_faithful: bool
    note: str = ""
    human_score: float | None = None  # 0, 0.5 or 1 when a person graded finer than yes or no
    source: str = ""


def load_cases(path: Path = CASES) -> list[CalibrationCase]:
    return [
        CalibrationCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def judge_passage(client: LLMClient, question: str, answer: str, passage: str) -> float:
    """The harness rubric applied to one answer and the passage it should rest on."""
    prompt = f"Question: {question}\n\nAnswer: {answer}\n\nCited passages:\n[p1]\n{passage}"
    j = await client.structured(
        prompt, Judgement, role=ModelRole.JUDGE, system=JUDGE_SYSTEM, max_tokens=300
    )
    return j.faithfulness


def agreement(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """(human, judge) pairs to agreement, Cohen's kappa and the two error rates."""
    raise NotImplementedError("Calibrate: agreement, kappa, false pass and fail rates")


def kappa_of(pairs: list[tuple[bool, bool]]) -> float:
    return agreement(pairs)["kappa"] if pairs else float("nan")


def intervals(report: dict[str, Any], pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Counts and 95 percent intervals for the decision metrics."""
    c = report["confusion"]
    lo, hi = bootstrap_ci(list(range(len(pairs))), lambda idx: kappa_of([pairs[i] for i in idx]))
    return {
        "agreement": proportion(c["tp"] + c["tn"], report["n"]),
        "false_pass": proportion(c["fp"], c["fp"] + c["tn"]),
        "false_fail": proportion(c["fn"], c["fn"] + c["tp"]),
        "kappa_ci": (lo, hi),
    }


def mean_calibration(cases: list[CalibrationCase], scores: list[float]) -> dict[str, Any]:
    """The gated metric: the judge's mean score against the human mean, per case paired."""
    human = [
        c.human_score if c.human_score is not None else (1.0 if c.human_faithful else 0.0)
        for c in cases
    ]
    diffs = [j - h for j, h in zip(scores, human, strict=True)]
    lo, hi = bootstrap_ci(diffs)
    return {
        "judge": statistics.mean(scores),
        "human": statistics.mean(human),
        "bias": statistics.mean(diffs),
        "bias_ci": (lo, hi),
        "mae": statistics.mean(abs(d) for d in diffs),
        "n": len(diffs),
    }


async def calibrate(
    cases: list[CalibrationCase], judge: JudgeFn, *, threshold: float = 0.75
) -> dict[str, Any]:
    scores = await asyncio.gather(*(judge(c.question, c.answer, c.passage) for c in cases))
    pairs = [(c.human_faithful, s >= threshold) for c, s in zip(cases, scores, strict=True)]
    report = agreement(pairs)
    report["threshold"] = threshold
    report["intervals"] = intervals(report, pairs)
    report["mean_score"] = mean_calibration(cases, list(scores))
    report["disagreements"] = [
        {"id": c.id, "human_faithful": c.human_faithful, "judge_score": s, "note": c.note}
        for c, s, (h, j) in zip(cases, scores, pairs, strict=True)
        if h != j
    ]
    return report


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"Judge calibration, n={r['n']}, threshold {r['threshold']:.2f}"
        + (f", judge {r['judge_model']}" if r.get("judge_model") else ""),
        f"  agreement        {r['agreement']:.3f}",
        f"  Cohen's kappa    {r['kappa']:.3f}",
        f"  false pass rate  {r['false_pass_rate']:.3f}   (unfaithful answers the judge passed)",
        f"  false fail rate  {r['false_fail_rate']:.3f}   (faithful answers the judge failed)",
    ]
    iv = r.get("intervals")
    if iv:
        lo, hi = iv["kappa_ci"]
        lines += [
            f"  agreement        {fmt_rate(iv['agreement'])}",
            f"  kappa 95% CI     {lo:.3f} to {hi:.3f}",
            f"  false pass       {fmt_rate(iv['false_pass'])}",
            f"  false fail       {fmt_rate(iv['false_fail'])}",
        ]
    m = r.get("mean_score")
    if m:
        lines.append(
            f"  mean score       judge {m['judge']:.3f}, human {m['human']:.3f}, bias "
            f"{m['bias']:+.3f} (95% CI {m['bias_ci'][0]:+.3f} to {m['bias_ci'][1]:+.3f}), "
            f"MAE {m['mae']:.3f}"
        )
    for d in r["disagreements"]:
        lines.append(
            f"  disagree {d['id']}: human={'faithful' if d['human_faithful'] else 'unfaithful'}"
            f" judge={d['judge_score']:.2f}  {d['note']}"
        )
    return "\n".join(lines)


def candidates_from_eval(
    report: dict[str, Any], chunks: dict[str, str], *, source: str
) -> list[dict[str, Any]]:
    """Unlabelled calibration cases from a live eval report: each answered case with the
    passages it cited. `human_faithful` is null until a person fills it in."""
    out = []
    for r in report.get("results") or []:
        if r.get("refused") or not r.get("answer") or not r.get("citations"):
            continue
        passage = "\n\n".join(chunks[c] for c in r["citations"] if c in chunks)
        if not passage:
            continue
        out.append(
            {
                "id": f"w-{r['id']}",
                "question": r.get("question", ""),
                "answer": r["answer"],
                "passage": passage,
                "human_faithful": None,
                "note": "",
                "source": source,
            }
        )
    return out


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    from nw.config import settings
    from nw.llm.providers import make_provider

    s = settings()
    if why := s.judge_unavailable():  # calibrating a canned Judge would measure nothing
        print(f"judged run refused: {why}")
        raise SystemExit(2)
    client = LLMClient(make_provider(s), settings=s)
    cases = load_cases(Path(args.cases))

    async def judge(q: str, a: str, p: str) -> float:
        return await judge_passage(client, q, a, p)

    report = await calibrate(cases, judge, threshold=args.threshold)
    report["cost_usd"] = client.spend_usd
    report["judge_model"] = s.model_for(ModelRole.JUDGE)
    report["judge_prompt"] = JUDGE_PROMPT.version
    report["track"] = s.track.value
    report["cases"] = str(args.cases)
    return report


def write_candidates(eval_path: Path, chunks_path: Path, golden: Path, out: Path) -> int:
    from nw.policy.evaluate import load_cases as load_golden

    report = json.loads(eval_path.read_text(encoding="utf-8"))
    questions = {c.id: c.question for c in load_golden(golden)}
    for r in report.get("results") or []:
        r["question"] = questions.get(r["id"], "")
    chunks = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            chunks[c["id"]] = c["text"]
    workhorse = (report.get("models") or {}).get("workhorse")
    rows = candidates_from_eval(
        report, chunks, source=f"{workhorse} answer, {report.get('written_at', '')[:10]}"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"{len(rows)} unlabelled cases from {workhorse} written to {out}; label human_faithful")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--threshold", type=float, default=0.75, help="judge score counted as a pass")
    ap.add_argument("--min-kappa", type=float, default=0.6, help="exit 1 below this agreement")
    ap.add_argument("--out", default="artifacts/policy/judge_calibration.json")
    ap.add_argument("--from-eval", type=Path, default=None, help="an eval.json to label")
    ap.add_argument(
        "--share",
        action="store_true",
        help="also write data/golden/calibrations/policy-judge-<track>.json for the cohort",
    )
    ap.add_argument("--chunks", type=Path, default=Path("artifacts/policy/chunks.jsonl"))
    ap.add_argument("--golden", type=Path, default=Path("data/golden/policy_qa.jsonl"))
    ap.add_argument(
        "--candidates", type=Path, default=Path("artifacts/policy/calibration_candidates.jsonl")
    )
    args = ap.parse_args()
    if args.from_eval:
        return write_candidates(args.from_eval, args.chunks, args.golden, args.candidates)
    report = asyncio.run(main_async(args))
    print(format_report(report))
    print(f"  cost             {report['cost_usd']:.4f} USD")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.share:
        shared = SHARED_CALIBRATIONS / f"policy-judge-{report['track']}.json"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"  shared report    {shared}: commit it and the cohort reads it")
    return 0 if report["kappa"] >= args.min_kappa else 1


if __name__ == "__main__":
    sys.exit(main())
