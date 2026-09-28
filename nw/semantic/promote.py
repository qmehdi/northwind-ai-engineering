"""The promotion gate for Project 2: a candidate is served only by passing it.

    uv run python -m nw.semantic.promote                       # newest candidate against production
    uv run python -m nw.semantic.promote --candidate <version>
    uv run python -m nw.semantic.promote --candidate <version> --force   # record the override

The gate reads three files the candidate must already carry: `metadata.json` from
training (the test evaluation), `export_report.json` from the export (parity) and
`benchmark.json` from the benchmark (the int8 graph's task metrics and latency). Export
and benchmark therefore run before the gate, and a missing file is named, not guessed.

Absolute bars protect Northwind: the tags the model exists for, a floor on P0 recall
(Project 1's threshold rule carries the SLA in front of this model), the fp32 graph
matching PyTorch, the int8 graph costing at most three points against fp32 on either
head, and a latency the service can afford. Relative bars protect against regressions
on the model currently in production, valid only on the same test split. A passed gate
points `artifacts/semantic/latest` at the candidate, moves the registry alias, and
rewrites the production summary the retraining workflow compares against in CI.
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
from nw.semantic.artifacts import newest_candidate

log = get_logger("nw.semantic.promote")
PRODUCTION_SUMMARY = Path("data/golden/semantic_production.json")


@dataclass
class GatePolicy:
    min_p0_recall: float = 0.70
    min_tag_micro_f1: float = 0.50
    max_parity_fp32: float = 1e-4
    max_int8_macro_f1_drop: float = 0.03  # priority macro-F1, int8 against fp32
    max_int8_tag_f1_drop: float = 0.03  # tag micro-F1, int8 against fp32
    max_int8_p95_ms: float = 100.0
    max_tag_micro_f1_drop: float = 0.02  # against production
    max_priority_macro_f1_drop: float = 0.02  # against production


@dataclass
class Decision:
    candidate: str
    production: str | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    forced: bool = False
    decided_at: str = ""


def _read(artifact_dir: Path, name: str, produced_by: str) -> Any:
    path = artifact_dir / name
    if not path.exists():
        raise SystemExit(
            f"{name} is missing from {artifact_dir}: {produced_by} must run before the gate"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _row(benchmark: list[dict[str, Any]], *names: str) -> dict[str, Any] | None:
    for name in names:
        for r in benchmark:
            if r["model"].endswith(name):
                return r
    return None


def summary(artifact_dir: Path) -> dict[str, Any]:
    """The production summary of one artifact: what the gate compares and what CI keeps."""
    meta = _read(artifact_dir, "metadata.json", "training")
    export = _read(artifact_dir, "export_report.json", "the export (`make export-semantic`)")
    bench = _read(artifact_dir, "benchmark.json", "the benchmark (`make benchmark`)")
    fp32 = _row(bench, "ONNX fp32", "PyTorch fp32")
    int8 = _row(bench, "ONNX int8")
    if fp32 is None or int8 is None:
        raise SystemExit(
            f"benchmark.json in {artifact_dir} has no fp32 and int8 rows: rerun the benchmark"
        )
    t = meta["metrics"]["test"]
    keys = ("tag_micro_f1", "tag_macro_f1", "priority_macro_f1", "p0_recall")
    return {
        "version": meta["version"],
        "data_sha256_12": meta["data_sha256_12"],
        "test": {k: t[k] for k in keys},
        "export": {
            "max_abs_diff_fp32": export["max_abs_diff_fp32"],
            "max_abs_diff_int8": export["max_abs_diff_int8"],
            "size_ratio": export["size_ratio"],
        },
        "benchmark": {
            "fp32": {k: fp32[k] for k in ("priority_macro_f1", "p0_recall", "tag_micro_f1")},
            "int8": {
                k: int8[k]
                for k in ("priority_macro_f1", "p0_recall", "tag_micro_f1", "p50_ms", "p95_ms")
            },
        },
    }


def gate(
    candidate: dict[str, Any], production: dict[str, Any] | None, policy: GatePolicy | None = None
) -> Decision:
    """Compare two production summaries (see `summary`); None means the first model."""
    policy = policy or GatePolicy()
    c, e, b = candidate["test"], candidate["export"], candidate["benchmark"]
    reasons: list[str] = []
    if c["p0_recall"] < policy.min_p0_recall:
        reasons.append(
            f"P0 recall {c['p0_recall']:.3f} is below the bar {policy.min_p0_recall:.2f}"
        )
    if c["tag_micro_f1"] < policy.min_tag_micro_f1:
        reasons.append(
            f"tag micro-F1 {c['tag_micro_f1']:.3f} is below the bar {policy.min_tag_micro_f1:.2f}"
        )
    if e["max_abs_diff_fp32"] > policy.max_parity_fp32:
        reasons.append(
            f"fp32 ONNX parity {e['max_abs_diff_fp32']:.2e} is above the bar "
            f"{policy.max_parity_fp32:.0e}"
        )
    drop = b["fp32"]["priority_macro_f1"] - b["int8"]["priority_macro_f1"]
    if drop > policy.max_int8_macro_f1_drop:
        reasons.append(
            f"int8 priority macro-F1 {b['int8']['priority_macro_f1']:.3f} drops more than "
            f"{policy.max_int8_macro_f1_drop:.2f} from fp32 {b['fp32']['priority_macro_f1']:.3f}"
        )
    tag_drop = b["fp32"]["tag_micro_f1"] - b["int8"]["tag_micro_f1"]
    if tag_drop > policy.max_int8_tag_f1_drop:
        reasons.append(
            f"int8 tag micro-F1 {b['int8']['tag_micro_f1']:.3f} drops more than "
            f"{policy.max_int8_tag_f1_drop:.2f} from fp32 {b['fp32']['tag_micro_f1']:.3f}"
        )
    if b["int8"]["p95_ms"] > policy.max_int8_p95_ms:
        reasons.append(
            f"int8 p95 latency {b['int8']['p95_ms']:.1f} ms is above the bar "
            f"{policy.max_int8_p95_ms:.0f} ms"
        )
    if production is not None:
        p = production["test"]
        if candidate.get("data_sha256_12") != production.get("data_sha256_12"):
            reasons.append(
                "the test split changed since production was measured; the comparison is "
                "not valid: retrain production on the new data or pass --force"
            )
        if c["tag_micro_f1"] < p["tag_micro_f1"] - policy.max_tag_micro_f1_drop:
            reasons.append(
                f"tag micro-F1 {c['tag_micro_f1']:.3f} drops more than "
                f"{policy.max_tag_micro_f1_drop:.2f} from {p['tag_micro_f1']:.3f}"
            )
        if c["priority_macro_f1"] < p["priority_macro_f1"] - policy.max_priority_macro_f1_drop:
            reasons.append(
                f"priority macro-F1 {c['priority_macro_f1']:.3f} drops more than "
                f"{policy.max_priority_macro_f1_drop:.2f} from {p['priority_macro_f1']:.3f}"
            )
    return Decision(
        candidate=candidate["version"],
        production=production["version"] if production else None,
        passed=not reasons,
        reasons=reasons,
        decided_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def current_production(out: Path, summary_path: Path = PRODUCTION_SUMMARY) -> dict[str, Any] | None:
    """The served model if the artifact tree has one, else the committed summary, else None."""
    latest = out / "latest"
    if (latest / "metadata.json").exists():
        return summary(latest)
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
    cand = summary(cand_dir)
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
        from nw.semantic.model_card import write as write_card
        from nw.semantic.tracking import set_production_alias

        write_card(cand_dir)  # now with the export, the benchmark and this decision
        set_production_alias(candidate_version)
        log.info("promoted %s (forced=%s)", candidate_version, decision.forced)
    else:
        log.warning("gate failed for %s: %s", candidate_version, "; ".join(decision.reasons))
    return decision


def format_decision(d: Decision) -> str:
    head = f"candidate {d.candidate} against production {d.production or 'none'}: " + (
        "PROMOTED (forced)" if d.forced else ("PROMOTED" if d.passed else "GATE FAILED")
    )
    return "\n".join([head, *(f"  {r}" for r in d.reasons)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--candidate", default=None, help="artifact version; default is the newest")
    ap.add_argument(
        "--force", action="store_true", help="promote despite a failed gate, recorded as forced"
    )
    ap.add_argument("--summary", type=Path, default=PRODUCTION_SUMMARY)
    args = ap.parse_args()
    version = args.candidate or newest_candidate(args.out).name
    d = promote(args.out, version, force=args.force, summary_path=args.summary)
    print(format_decision(d))
    return 0 if d.passed else 1


if __name__ == "__main__":
    sys.exit(main())
