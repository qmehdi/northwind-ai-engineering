"""Calibrate the Judge against human labels before trusting its faithfulness score.

A judge model is a measurement instrument. Like any instrument it needs calibrating: a small
set of answers a person has labelled faithful or not, run through the same rubric the harness
uses, and a report of how often the two agree. The number that matters most is the false
pass rate, the share of unfaithful answers the judge waved through, because that is the
failure the regression gate cannot see.

    uv run python -m nw.policy.calibrate                # Judge role on your track, about 0.10 USD
    uv run python -m nw.policy.calibrate --threshold 0.5

Cases live in `data/golden/judge_calibration.jsonl` and carry their own passage text, so they
stay valid when the chunker or the corpus changes. Add a case whenever the judge surprises you.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nw.config import ModelRole
from nw.llm import LLMClient
from nw.policy.evaluate import JUDGE_SYSTEM, Judgement

CASES = Path("data/golden/judge_calibration.jsonl")
JudgeFn = Callable[[str, str, str], Awaitable[float]]


class CalibrationCase(BaseModel):
    id: str
    question: str
    answer: str
    passage: str
    human_faithful: bool
    note: str = ""


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
    raise NotImplementedError("Step 6: agreement, kappa, false pass and false fail rates")


async def calibrate(
    cases: list[CalibrationCase], judge: JudgeFn, *, threshold: float = 0.75
) -> dict[str, Any]:
    scores = await asyncio.gather(*(judge(c.question, c.answer, c.passage) for c in cases))
    pairs = [(c.human_faithful, s >= threshold) for c, s in zip(cases, scores, strict=True)]
    report = agreement(pairs)
    report["threshold"] = threshold
    report["disagreements"] = [
        {"id": c.id, "human_faithful": c.human_faithful, "judge_score": s, "note": c.note}
        for c, s, (h, j) in zip(cases, scores, pairs, strict=True)
        if h != j
    ]
    return report


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"Judge calibration, n={r['n']}, threshold {r['threshold']:.2f}",
        f"  agreement        {r['agreement']:.3f}",
        f"  Cohen's kappa    {r['kappa']:.3f}",
        f"  false pass rate  {r['false_pass_rate']:.3f}   (unfaithful answers the judge passed)",
        f"  false fail rate  {r['false_fail_rate']:.3f}   (faithful answers the judge failed)",
    ]
    for d in r["disagreements"]:
        lines.append(
            f"  disagree {d['id']}: human={'faithful' if d['human_faithful'] else 'unfaithful'}"
            f" judge={d['judge_score']:.2f}  {d['note']}"
        )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    client = LLMClient()
    cases = load_cases(Path(args.cases))

    async def judge(q: str, a: str, p: str) -> float:
        return await judge_passage(client, q, a, p)

    report = await calibrate(cases, judge, threshold=args.threshold)
    report["cost_usd"] = client.spend_usd
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--threshold", type=float, default=0.75, help="judge score counted as a pass")
    ap.add_argument("--min-kappa", type=float, default=0.6, help="exit 1 below this agreement")
    ap.add_argument("--out", default="artifacts/policy/judge_calibration.json")
    args = ap.parse_args()
    report = asyncio.run(main_async(args))
    print(format_report(report))
    print(f"  cost             {report['cost_usd']:.4f} USD")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["kappa"] >= args.min_kappa else 1


if __name__ == "__main__":
    sys.exit(main())
