"""Project 1, step 3: the promotion gate against the committed production summary.

    python -m nw.pipelines.steps.triage_evaluate --out artifacts/triage \
        --production-summary data/golden/triage_production.json --min-p0-recall 0.85

The same `gate` as `nw.triage.promote`, with the bars as arguments so a retraining job can
tighten them. Nothing moves `latest` here: a pipeline registers the candidate and a human
approves it in the registry. The decision is written twice, into the version directory and
into `steps/triage_evaluate.json`, whose `passed_int` a SageMaker ConditionStep compares.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.steps import add_flag, localize, truthy, write_json, write_result
from nw.triage.promote import (
    PRODUCTION_SUMMARY,
    GatePolicy,
    current_production,
    format_decision,
    gate,
    newest_candidate,
    read_metadata,
    summary,
)

STEP = "triage_evaluate"


def run(
    out: Path,
    version: str | None = None,
    *,
    production_summary: Path = PRODUCTION_SUMMARY,
    policy: GatePolicy | None = None,
    force: bool = False,
) -> dict[str, Any]:
    out = Path(out)
    version = version or newest_candidate(out)
    candidate = summary(read_metadata(out / version))
    production = current_production(out, localize(production_summary, ".json"))
    if production and production["version"] == candidate["version"]:
        production = None
    decision = gate(candidate, production, policy)
    if force and not decision.passed:
        decision.passed, decision.forced = True, True
    result = {
        "step": STEP,
        "pipeline": "triage",
        **asdict(decision),
        "passed_int": int(decision.passed),
        "reason": "; ".join(decision.reasons) or "all bars cleared",
        "data_sha256_12": candidate["data_sha256_12"],
        "metrics": {**candidate["test"], "p0_threshold": candidate["p0_threshold"]},
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
    ap.add_argument("--max-ece", type=float, default=d.max_ece)
    ap.add_argument("--max-macro-f1-drop", type=float, default=d.max_macro_f1_drop)
    ap.add_argument("--max-brier-increase", type=float, default=d.max_brier_increase)
    ap.add_argument("--max-p0-recall-drop", type=float, default=d.max_p0_recall_drop)


def policy_from(args: argparse.Namespace) -> GatePolicy:
    return GatePolicy(
        min_p0_recall=args.min_p0_recall,
        max_ece=args.max_ece,
        max_macro_f1_drop=args.max_macro_f1_drop,
        max_brier_increase=args.max_brier_increase,
        max_p0_recall_drop=args.max_p0_recall_drop,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
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
