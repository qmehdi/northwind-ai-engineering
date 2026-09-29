"""Project 1, step 2: train a candidate.

    python -m nw.pipelines.steps.triage_train --data data/tickets.jsonl --out artifacts/triage

`nw.triage.train.train` with `promote=False`: the versioned artifact, the run in `runs.jsonl`
and in MLflow when installed, the model card. The gate is the next step, never this one.
`--package-dir` also writes `model.tar.gz`, the shape SageMaker's register step ingests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from nw.pipelines.steps import localize, package, write_result
from nw.triage.train import format_report, train

STEP = "triage_train"


def run(
    data: Path,
    out: Path,
    *,
    target_recall: float = 0.90,
    min_precision: float = 0.25,
    class_weight: str | None = "balanced",
    calibrate: bool = True,
    seed: int = 0,
    package_dir: Path | None = None,
) -> dict[str, Any]:
    model, report = train(
        localize(data),
        Path(out),
        class_weight=class_weight,
        calibrate=calibrate,
        target_recall=target_recall,
        min_precision=min_precision,
        seed=seed,
        promote=False,
    )
    test = report["test"]
    result = {
        "step": STEP,
        "version": model.version,
        "artifact": str(Path(out) / model.version),
        "data_sha256_12": model.metadata["data_sha256_12"],
        "p0_threshold": report["p0_threshold"],
        "metrics": {
            "macro_f1": test["macro_f1"],
            "p0_recall": test["p0_recall"],
            "p0_precision": test["p0_precision"],
            "ece": test["ece"],
            "brier_p0": test["brier_p0"],
        },
        "packaged": None,
    }
    if package_dir is not None:
        result["packaged"] = str(package(Path(out) / model.version, package_dir))
    write_result(out, STEP, result)
    print(f"model {model.version}\n")
    print(format_report(report))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
    ap.add_argument("--target-recall", type=float, default=0.90)
    ap.add_argument("--min-precision", type=float, default=0.25)
    ap.add_argument("--no-class-weight", action="store_true")
    ap.add_argument("--no-calibration", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--package-dir", type=Path, default=None, help="also write model.tar.gz into this directory"
    )
    args = ap.parse_args(argv)
    run(
        args.data,
        args.out,
        target_recall=args.target_recall,
        min_precision=args.min_precision,
        class_weight=None if args.no_class_weight else "balanced",
        calibrate=not args.no_calibration,
        seed=args.seed,
        package_dir=args.package_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
