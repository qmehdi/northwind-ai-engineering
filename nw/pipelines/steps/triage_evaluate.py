"""Project 1, step 3: the promotion gate against the committed production summary.

    python -m nw.pipelines.steps.triage_evaluate --out artifacts/triage \
        --production-summary data/golden/triage_production.json --min-p0-recall 0.85

The same `gate` as `nw.triage.promote`, with the bars as arguments so a retraining job can
tighten them. Nothing moves `latest` here: a pipeline registers the candidate and a human
approves it in the registry. The decision is written twice, into the version directory and
into `steps/triage_evaluate.json`, whose `passed_int` a SageMaker ConditionStep compares.

The champion (`--champion`, `nw.pipelines.champion`): `registry` compares against the tenant's
live version, else the production summary; `summary` against the summary file only. A named
`--production-summary` that does not exist fails the step (`none` means a first model on
purpose); URIs are strings, so `gs://` survives the command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.champion import CHAMPIONS, choose, tenant_for_steps
from nw.pipelines.steps import (
    add_flag,
    local_path,
    truthy,
    write_json,
    write_result,
)
from nw.platform.base import ModelRegistry, Tenant
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


def _summarise(artifact: Path) -> dict[str, Any]:
    return summary(read_metadata(artifact))


def run(
    out: Path | str,
    version: str | None = None,
    *,
    production_summary: str | Path | None = None,
    policy: GatePolicy | None = None,
    force: bool = False,
    champion: str = "summary",
    registry: ModelRegistry | None = None,
    tenant: Tenant | None = None,
) -> dict[str, Any]:
    out = local_path(out)
    version = version or newest_candidate(out)
    candidate = summary(read_metadata(out / version))
    production, champion_source = choose(
        "triage",
        out,
        production_summary,
        champion,
        summarise=_summarise,
        default=PRODUCTION_SUMMARY,
        current=current_production,
        registry=registry,
        tenant=tenant,
    )
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
        "production_summary": str(production_summary or PRODUCTION_SUMMARY),
        "champion_source": champion_source,
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
    ap.add_argument("--out", default="artifacts/triage", help="the run's tree: a path or gs:// URI")
    ap.add_argument("--version", default=None, help="artifact version; default is the newest")
    ap.add_argument(
        "--production-summary",
        default=None,
        help=f"a path or URI that must exist, or `none`; default {PRODUCTION_SUMMARY} if present",
    )
    ap.add_argument("--champion", choices=CHAMPIONS, default="summary")
    ap.add_argument("--tenant", default=None, help="whose live version is the champion")
    ap.add_argument("--environment", default=None)
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
        champion=args.champion,
        tenant=tenant_for_steps(args.tenant, args.environment)
        if args.champion == "registry"
        else None,
    )
    return 1 if args.strict and not result["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
