"""The promotion gate: a candidate becomes the served model only by passing it.

    uv run python -m nw.triage.promote                       # newest candidate against production
    uv run python -m nw.triage.promote --candidate <version>
    uv run python -m nw.triage.promote --candidate <version> --force   # record the override

Absolute bars protect Northwind (P0 recall, calibration); relative bars protect against
regressions (macro-F1 and Brier against the model currently in production). The
comparison is only valid on the same test split, so a data change is a finding, not a
silent pass. A passed gate points `artifacts/triage/latest` at the candidate, moves the
registry alias, and writes the production summary the retraining workflow compares
against in CI, where no artifact directory exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nw.logging import get_logger

log = get_logger("nw.triage.promote")
PRODUCTION_SUMMARY = Path("data/golden/triage_production.json")


@dataclass
class GatePolicy:
    min_p0_recall: float = 0.85
    max_ece: float = 0.12
    max_macro_f1_drop: float = 0.02
    max_brier_increase: float = 0.01
    max_p0_recall_drop: float = 0.03


@dataclass
class Decision:
    candidate: str
    production: str | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    forced: bool = False
    decided_at: str = ""


def _summary(metadata: dict[str, Any]) -> dict[str, Any]:
    t = metadata["metrics"]["test"]
    return {
        "version": metadata["version"],
        "data_sha256_12": metadata["data_sha256_12"],
        "test": {k: t[k] for k in ("macro_f1", "p0_recall", "p0_precision", "ece", "brier_p0")},
        "p0_threshold": metadata["metrics"]["p0_threshold"],
    }


def gate(
    candidate: dict[str, Any], production: dict[str, Any] | None, policy: GatePolicy | None = None
) -> Decision:
    """Compare two production summaries (see `_summary`); None means the first model."""
    policy = policy or GatePolicy()
    c = candidate["test"]
    reasons: list[str] = []
    if c["p0_recall"] < policy.min_p0_recall:
        reasons.append(
            f"P0 recall {c['p0_recall']:.3f} is below the bar {policy.min_p0_recall:.2f}"
        )
    if c["ece"] > policy.max_ece:
        reasons.append(f"ECE {c['ece']:.3f} is above the bar {policy.max_ece:.2f}")
    if production is not None:
        p = production["test"]
        if candidate.get("data_sha256_12") != production.get("data_sha256_12"):
            reasons.append(
                "the test split changed since production was measured; the comparison is "
                "not valid: retrain production on the new data or pass --force"
            )
        if c["macro_f1"] < p["macro_f1"] - policy.max_macro_f1_drop:
            reasons.append(
                f"macro-F1 {c['macro_f1']:.3f} drops more than "
                f"{policy.max_macro_f1_drop:.2f} from {p['macro_f1']:.3f}"
            )
        if c["p0_recall"] < p["p0_recall"] - policy.max_p0_recall_drop:
            reasons.append(
                f"P0 recall {c['p0_recall']:.3f} drops more than "
                f"{policy.max_p0_recall_drop:.2f} from {p['p0_recall']:.3f}"
            )
        if c["brier_p0"] > p["brier_p0"] + policy.max_brier_increase:
            reasons.append(
                f"Brier {c['brier_p0']:.4f} worsens more than "
                f"{policy.max_brier_increase:.3f} from {p['brier_p0']:.4f}"
            )
    return Decision(
        candidate=candidate["version"],
        production=production["version"] if production else None,
        passed=not reasons,
        reasons=reasons,
        decided_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def read_metadata(artifact_dir: Path) -> dict[str, Any]:
    return json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))


def current_production(out: Path, summary_path: Path = PRODUCTION_SUMMARY) -> dict[str, Any] | None:
    """The served model if the artifact tree has one, else the committed summary, else None."""
    latest = out / "latest"
    if (latest / "metadata.json").exists():
        return _summary(read_metadata(latest))
    if summary_path.exists():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    return None


def promote(
    out: Path,
    candidate_version: str,
    *,
    force: bool = False,
    policy: GatePolicy | None = None,
    summary_path: Path = PRODUCTION_SUMMARY,
    write_summary: bool = True,
) -> Decision:
    cand_dir = out / candidate_version
    cand = _summary(read_metadata(cand_dir))
    prod = current_production(out, summary_path)
    if prod and prod["version"] == cand["version"]:
        prod = None  # promoting the served model again is a no-op comparison
    decision = gate(cand, prod, policy)
    if force and not decision.passed:
        decision.passed, decision.forced = True, True
    (out / "promotions.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with (out / "promotions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(decision)) + "\n")
    if decision.passed:
        latest = out / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(candidate_version, target_is_directory=True)
        if write_summary:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps({**cand, "promoted_at": decision.decided_at}, indent=1) + "\n"
            )
        from nw.triage.tracking import set_production_alias

        set_production_alias(candidate_version)
        log.info("promoted %s (forced=%s)", candidate_version, decision.forced)
    else:
        log.warning("gate failed for %s: %s", candidate_version, "; ".join(decision.reasons))
    return decision


def newest_candidate(out: Path) -> str:
    versions = sorted(
        p.name
        for p in out.iterdir()
        if p.is_dir() and not p.is_symlink() and (p / "metadata.json").exists()
    )
    if not versions:
        raise SystemExit(f"no candidate under {out}")
    return versions[-1]


def format_decision(d: Decision) -> str:
    head = f"candidate {d.candidate} against production {d.production or 'none'}: " + (
        "PROMOTED (forced)" if d.forced else ("PROMOTED" if d.passed else "GATE FAILED")
    )
    return "\n".join([head, *(f"  {r}" for r in d.reasons)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
    ap.add_argument("--candidate", default=None, help="artifact version; default is the newest")
    ap.add_argument(
        "--force", action="store_true", help="promote despite a failed gate, recorded as forced"
    )
    ap.add_argument("--summary", type=Path, default=PRODUCTION_SUMMARY)
    args = ap.parse_args()
    version = args.candidate or newest_candidate(args.out)
    d = promote(args.out, version, force=args.force, summary_path=args.summary)
    print(format_decision(d))
    return 0 if d.passed else 1


if __name__ == "__main__":
    sys.exit(main())
