"""Project 2, step 5: the promotion gate against the committed production summary.

    python -m nw.pipelines.steps.semantic_gate --out artifacts/semantic \
        --production-summary data/golden/semantic_production.json

The same `gate` as `nw.semantic.promote`, fed by the export report and the benchmark the
previous steps wrote, with the bars as arguments. Nothing moves `latest`: the register step
and a human approval do that. The decision is written into the version directory and into
`steps/semantic_gate.json`, whose `passed_int` a SageMaker ConditionStep compares.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.steps import add_flag, localize, truthy, write_json, write_result
from nw.semantic.artifacts import newest_candidate
from nw.semantic.promote import (
    PRODUCTION_SUMMARY,
    GatePolicy,
    current_production,
    format_decision,
    gate,
    summary,
)

STEP = "semantic_gate"


def run(
    out: Path,
    version: str | None = None,
    *,
    production_summary: Path = PRODUCTION_SUMMARY,
    policy: GatePolicy | None = None,
    force: bool = False,
) -> dict[str, Any]:
    out = Path(out)
    version = version or newest_candidate(out).name
    candidate = summary(out / version)
    production = current_production(out, localize(production_summary, ".json"))
    if production and production["version"] == candidate["version"]:
        production = None
    decision = gate(candidate, production, policy)
    if force and not decision.passed:
        decision.passed, decision.forced = True, True
    result = {
        "step": STEP,
        "pipeline": "semantic",
        **asdict(decision),
        "passed_int": int(decision.passed),
        "reason": "; ".join(decision.reasons) or "all bars cleared",
        "data_sha256_12": candidate["data_sha256_12"],
        "metrics": {
            **candidate["test"],
            "int8_p95_ms": candidate["benchmark"]["int8"]["p95_ms"],
            "int8_tag_micro_f1": candidate["benchmark"]["int8"]["tag_micro_f1"],
            "int8_priority_macro_f1": candidate["benchmark"]["int8"]["priority_macro_f1"],
            "max_abs_diff_fp32": candidate["export"]["max_abs_diff_fp32"],
        },
        "production_summary": str(production_summary),
    }
    write_json(out / version / "gate.json", result)
    write_result(out, STEP, result)
    with (out / "promotions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**asdict(decision), "step": STEP}) + "\n")
    print(format_decision(decision))
    return result


def add_policy_arguments(ap: argparse.ArgumentParser) -> None:
    d = GatePolicy()
    ap.add_argument("--min-p0-recall", type=float, default=d.min_p0_recall)
    ap.add_argument("--min-tag-micro-f1", type=float, default=d.min_tag_micro_f1)
    ap.add_argument("--max-parity-fp32", type=float, default=d.max_parity_fp32)
    ap.add_argument("--max-int8-macro-f1-drop", type=float, default=d.max_int8_macro_f1_drop)
    ap.add_argument("--max-int8-tag-f1-drop", type=float, default=d.max_int8_tag_f1_drop)
    ap.add_argument("--max-int8-p95-ms", type=float, default=d.max_int8_p95_ms)
    ap.add_argument("--max-tag-micro-f1-drop", type=float, default=d.max_tag_micro_f1_drop)
    ap.add_argument(
        "--max-priority-macro-f1-drop", type=float, default=d.max_priority_macro_f1_drop
    )


def policy_from(args: argparse.Namespace) -> GatePolicy:
    return GatePolicy(
        min_p0_recall=args.min_p0_recall,
        min_tag_micro_f1=args.min_tag_micro_f1,
        max_parity_fp32=args.max_parity_fp32,
        max_int8_macro_f1_drop=args.max_int8_macro_f1_drop,
        max_int8_tag_f1_drop=args.max_int8_tag_f1_drop,
        max_int8_p95_ms=args.max_int8_p95_ms,
        max_tag_micro_f1_drop=args.max_tag_micro_f1_drop,
        max_priority_macro_f1_drop=args.max_priority_macro_f1_drop,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--version", default=None, help="artifact version; default is the newest")
    ap.add_argument("--production-summary", type=Path, default=PRODUCTION_SUMMARY)
    add_flag(ap, "--force", "pass the gate anyway, recorded as forced")
    ap.add_argument(
        "--strict", action="store_true", help="exit 1 on a failed gate instead of recording it"
    )
    add_policy_arguments(ap)
    args = ap.parse_args(argv)
    result = run(
        args.out,
        args.version,
        production_summary=args.production_summary,
        policy=policy_from(args),
        force=truthy(args.force),
    )
    return 1 if args.strict and not result["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
