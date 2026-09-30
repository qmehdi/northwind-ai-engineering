"""The promotion gate for Project 2: a candidate is served only by passing it.

    uv run python -m nw.semantic.promote                       # newest candidate against production
    uv run python -m nw.semantic.promote --candidate <version>
    uv run python -m nw.semantic.promote --serve fp32          # force the served format
    uv run python -m nw.semantic.promote --candidate <version> --force --reason "..."

The gate reads three files the candidate must already carry: `metadata.json` from
training (the test evaluation), `export_report.json` from the export (parity) and
`benchmark.json` from the benchmark (the int8 graph's task metrics and latency). Export
and benchmark therefore run before the gate, and a missing file is named, not guessed.

The absolute bars apply to the artifact that will be served, not to the PyTorch model it
came from. The service runs the int8 graph by default, and dynamic int8 quantisation costs
the rare class first: the first committed candidate kept 23 of 31 test P0 tickets in fp32
and 20 of 31 in int8, below the 0.70 floor. So the gate measures both graphs against the
served bars (P0 recall, tag micro-F1) and the int8 graph also against fp32 (macro-F1 and tag
drops, missed P0 tickets counted, not a rate under one ticket, and p95 latency). With the
default `--serve auto` it serves int8 when int8 clears every bar, else fp32 when fp32 does,
else fails; the choice goes into `serving.json` beside the artifact and into the production
summary, and the service reads it (`NW_QUANTIZED` still overrides).

Per language, a slice needs `min_slice_p0` P0 tickets before its P0 bar applies (the test
split holds two German P0 tickets: `insufficient evidence`, stated), and a slice with at
least `min_slice_rows` rows must not lose more than `max_slice_macro_f1_drop` priority
macro-F1 against production. Relative bars protect against regressions on the model
currently in production, valid only on the same test split. A passed gate points
`artifacts/semantic/latest` at the candidate, moves the registry alias, and rewrites the
production summary the retraining workflow compares against in CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nw.evalstats import as_count, fmt_rate, proportion
from nw.logging import get_logger
from nw.semantic.artifacts import newest_candidate
from nw.triage.promote import actor

log = get_logger("nw.semantic.promote")
PRODUCTION_SUMMARY = Path("data/golden/semantic_production.json")
FORMATS = ("int8", "fp32")


@dataclass
class GatePolicy:
    min_p0_recall: float = 0.70  # on the served graph
    min_tag_micro_f1: float = 0.50  # on the served graph
    max_parity_fp32: float = 1e-4
    max_int8_macro_f1_drop: float = 0.03  # priority macro-F1, int8 against fp32
    max_int8_tag_f1_drop: float = 0.03  # tag micro-F1, int8 against fp32
    max_int8_p0_missed_increase: int = 1  # test P0 tickets int8 misses that fp32 caught, net
    max_int8_p95_ms: float = 100.0
    max_tag_micro_f1_drop: float = 0.02  # against production
    max_priority_macro_f1_drop: float = 0.02  # against production
    min_slice_p0: int = 10
    min_slice_rows: int = 30
    max_slice_macro_f1_drop: float = 0.05  # per language, against production
    serve: str = "auto"  # auto, int8 or fp32


@dataclass
class Decision:
    candidate: str
    production: str | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    forced: bool = False
    decided_at: str = ""
    served_format: str | None = None
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    insufficient_evidence: list[str] = field(default_factory=list)
    decided_by: str = ""
    reason: str = ""


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
    n_p0 = t.get("n_p0") or as_count(t["p0_recall"])[1]
    out = {
        "version": meta["version"],
        "data_sha256_12": meta["data_sha256_12"],
        "test": {k: t[k] for k in keys},
        "test_counts": {"n": t.get("n"), "n_p0": n_p0},
        "by_language": {
            lang: {
                k: r.get(k) for k in ("n", "n_p0", "priority_macro_f1", "tag_micro_f1", "p0_recall")
            }
            for lang, r in (t.get("by_language") or {}).items()
        },
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
    _fill_slice_p0(out["by_language"], n_p0)
    served = artifact_dir / "serving.json"
    if served.exists():
        out["served_format"] = json.loads(served.read_text(encoding="utf-8")).get("format")
    return out


def _fill_slice_p0(by_language: dict[str, dict[str, Any]], n_p0: int) -> None:
    """Training records rates per language, not counts. Recover each slice's P0 count from
    its recall (23/31 is exact); a slice at recall 0 or 1 is ambiguous, and when exactly one
    slice is, it holds the remainder of the test split's P0 tickets."""
    unknown = []
    for lang, r in by_language.items():
        if r.get("n_p0") is not None:
            continue
        rate = r.get("p0_recall")
        if rate is None:
            r["n_p0"] = 0
        elif 0.0 < rate < 1.0:
            r["n_p0"] = as_count(rate, max_den=max(n_p0, 1))[1]
        else:
            unknown.append(lang)
    if len(unknown) == 1:
        known = sum(int(r["n_p0"]) for lang, r in by_language.items() if lang not in unknown)
        by_language[unknown[0]]["n_p0"] = max(n_p0 - known, 0)


def _slice_n_p0(r: dict[str, Any]) -> int:
    if r.get("n_p0") is not None:
        return int(r["n_p0"])
    return as_count(r["p0_recall"])[1] if r.get("p0_recall") else 0


def served_reasons(
    candidate: dict[str, Any], fmt: str, policy: GatePolicy
) -> tuple[list[str], dict[str, Any]]:
    """The bars one served format must clear, and the evidence behind them."""
    b = candidate["benchmark"]
    n_p0 = int(candidate.get("test_counts", {}).get("n_p0") or as_count(b["fp32"]["p0_recall"])[1])
    m = b[fmt]
    k = round(m["p0_recall"] * n_p0)
    ev = {"p0_recall": proportion(k, n_p0), "tag_micro_f1": m["tag_micro_f1"]}
    reasons: list[str] = []
    if m["p0_recall"] < policy.min_p0_recall:
        reasons.append(
            f"{fmt} P0 recall {fmt_rate(ev['p0_recall'])} is below the bar "
            f"{policy.min_p0_recall:.2f}"
        )
    if m["tag_micro_f1"] < policy.min_tag_micro_f1:
        reasons.append(
            f"{fmt} tag micro-F1 {m['tag_micro_f1']:.3f} is below the bar "
            f"{policy.min_tag_micro_f1:.2f}"
        )
    if fmt == "int8":
        f = b["fp32"]
        drop = f["priority_macro_f1"] - m["priority_macro_f1"]
        if drop > policy.max_int8_macro_f1_drop:
            reasons.append(
                f"int8 priority macro-F1 {m['priority_macro_f1']:.3f} drops more than "
                f"{policy.max_int8_macro_f1_drop:.2f} from fp32 {f['priority_macro_f1']:.3f}"
            )
        tag_drop = f["tag_micro_f1"] - m["tag_micro_f1"]
        if tag_drop > policy.max_int8_tag_f1_drop:
            reasons.append(
                f"int8 tag micro-F1 {m['tag_micro_f1']:.3f} drops more than "
                f"{policy.max_int8_tag_f1_drop:.2f} from fp32 {f['tag_micro_f1']:.3f}"
            )
        missed = round(f["p0_recall"] * n_p0) - k
        ev["p0_missed_vs_fp32"] = missed
        if missed > policy.max_int8_p0_missed_increase:
            reasons.append(
                f"int8 misses {missed} test P0 tickets that fp32 catches "
                f"({k}/{n_p0} against {round(f['p0_recall'] * n_p0)}/{n_p0}); the bar allows "
                f"{policy.max_int8_p0_missed_increase}"
            )
        if m["p95_ms"] > policy.max_int8_p95_ms:
            reasons.append(
                f"int8 p95 latency {m['p95_ms']:.1f} ms is above the bar "
                f"{policy.max_int8_p95_ms:.0f} ms"
            )
    return reasons, ev


def gate(
    candidate: dict[str, Any], production: dict[str, Any] | None, policy: GatePolicy | None = None
) -> Decision:
    """Compare two production summaries (see `summary`); None means the first model."""
    policy = policy or GatePolicy()
    c, e = candidate["test"], candidate["export"]
    reasons: list[str] = []
    notes: list[str] = []
    insufficient: list[str] = []
    evidence: dict[str, Any] = {}
    if e["max_abs_diff_fp32"] > policy.max_parity_fp32:
        reasons.append(
            f"fp32 ONNX parity {e['max_abs_diff_fp32']:.2e} is above the bar "
            f"{policy.max_parity_fp32:.0e}"
        )
    by_format = {fmt: served_reasons(candidate, fmt, policy) for fmt in FORMATS}
    evidence["served"] = {fmt: ev for fmt, (_, ev) in by_format.items()}
    if policy.serve in FORMATS:
        served = policy.serve
        reasons += by_format[served][0]
    elif not by_format["int8"][0]:
        served = "int8"
    elif not by_format["fp32"][0]:
        served = "fp32"
        notes.append(
            "int8 does not clear the served bars ("
            + "; ".join(by_format["int8"][0])
            + "): the fp32 graph is served"
        )
    else:
        served = "int8"
        reasons += by_format["int8"][0]
        reasons += [r for r in by_format["fp32"][0] if r not in reasons]
    for lang, r in sorted((candidate.get("by_language") or {}).items()):
        n_p0 = _slice_n_p0(r)
        if n_p0 < policy.min_slice_p0:
            insufficient.append(
                f"{lang}: {n_p0} P0 tickets in the test split, fewer than {policy.min_slice_p0}: "
                "P0 recall reported, not gated"
            )
        elif r.get("p0_recall") is not None and r["p0_recall"] < policy.min_p0_recall:
            reasons.append(
                f"{lang} P0 recall {fmt_rate(proportion(round(r['p0_recall'] * n_p0), n_p0))} "
                f"is below the bar {policy.min_p0_recall:.2f}"
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
        prod_langs = production.get("by_language") or {}
        for lang, r in sorted((candidate.get("by_language") or {}).items()):
            base = prod_langs.get(lang)
            if not base or (r.get("n") or 0) < policy.min_slice_rows:
                continue
            if r["priority_macro_f1"] < base["priority_macro_f1"] - policy.max_slice_macro_f1_drop:
                reasons.append(
                    f"{lang} priority macro-F1 {r['priority_macro_f1']:.3f} drops more than "
                    f"{policy.max_slice_macro_f1_drop:.2f} from {base['priority_macro_f1']:.3f}"
                )
        if not prod_langs:
            notes.append("production carries no per-language metrics: slice drops not compared")
    return Decision(
        candidate=candidate["version"],
        production=production["version"] if production else None,
        passed=not reasons,
        reasons=reasons,
        decided_at=dt.datetime.now(dt.UTC).isoformat(),
        served_format=served,
        notes=notes,
        evidence=evidence,
        insufficient_evidence=insufficient,
    )


recheck = gate  # an approval in a registry calls this again on the stored summaries


def write_serving(version_dir: Path, decision: Any) -> None:
    """`serving.json` beside a promoted version: the graph the gate chose to serve. The service,
    the agent's semantic tool and the backtest read it; it travels with the artifact into the
    registry, so a version served from there (`NW_MODEL_URI`) loads the same graph."""
    (version_dir / "serving.json").write_text(
        json.dumps(
            {
                "format": decision.served_format,
                "quantized": decision.served_format == "int8",
                "decided_at": decision.decided_at,
                "decided_by": decision.decided_by,
            },
            indent=1,
        )
    )


def served_quantized(artifact: Path) -> bool | None:
    """What the gate chose for this artifact (`serving.json`): True for int8, False for fp32,
    None when the artifact was never promoted here. The service reads it at startup;
    `NW_QUANTIZED` still overrides."""
    from nw.semantic.artifacts import resolve

    try:
        path = Path(resolve(artifact, serve=True)) / "serving.json"
    except (FileNotFoundError, OSError):
        return None
    if not path.exists():
        return None
    return bool(json.loads(path.read_text(encoding="utf-8")).get("quantized", True))


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
    by: str | None = None,
    reason: str = "",
) -> Decision:
    if force and not reason.strip():
        raise SystemExit("--force needs --reason: an override nobody explained is not an override")
    cand_dir = out / candidate_version
    cand = summary(cand_dir)
    prod = current_production(out, summary_path)
    if prod and prod["version"] == cand["version"]:
        prod = None  # promoting the served model again is a no-op comparison
    decision = gate(cand, prod, policy)
    decision.decided_by = by or actor()
    decision.reason = reason
    if force and not decision.passed:
        decision.passed, decision.forced = True, True
    (cand_dir / "promotion.json").write_text(json.dumps(asdict(decision), indent=1))
    (out / "promotions.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with (out / "promotions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(decision)) + "\n")
    if decision.passed:
        write_serving(cand_dir, decision)
        cand["served_format"] = decision.served_format
        latest = out / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(candidate_version, target_is_directory=True)
        if write_summary:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps(
                    {
                        **cand,
                        "promoted_at": decision.decided_at,
                        "promoted_by": decision.decided_by,
                        "forced": decision.forced,
                    },
                    indent=1,
                )
                + "\n"
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
    lines = [head, *(f"  {r}" for r in d.reasons)]
    if d.passed and d.served_format:
        lines.append(f"  serves the {d.served_format} graph")
    for fmt, ev in (d.evidence.get("served") or {}).items():
        lines.append(f"  evidence: {fmt} P0 recall {fmt_rate(ev['p0_recall'])}")
    lines += [f"  note: {n}" for n in d.notes]
    lines += [f"  insufficient evidence: {x}" for x in d.insufficient_evidence]
    if d.decided_by:
        lines.append(f"  decided by {d.decided_by}" + (f": {d.reason}" if d.reason else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--candidate", default=None, help="artifact version; default is the newest")
    ap.add_argument(
        "--force", action="store_true", help="promote despite a failed gate, recorded as forced"
    )
    ap.add_argument("--summary", type=Path, default=PRODUCTION_SUMMARY)
    ap.add_argument(
        "--serve",
        choices=("auto", *FORMATS),
        default=os.environ.get("NW_SEMANTIC_SERVE", "auto"),
        help="the graph to serve: auto picks int8 when it clears every bar, else fp32",
    )
    ap.add_argument("--by", default=None, help="who promotes; default NW_ACTOR or the login")
    ap.add_argument("--reason", default="", help="why; required with --force")
    args = ap.parse_args()
    version = args.candidate or newest_candidate(args.out).name
    d = promote(
        args.out,
        version,
        force=args.force,
        policy=GatePolicy(serve=args.serve),
        summary_path=args.summary,
        by=args.by,
        reason=args.reason,
    )
    print(format_decision(d))
    return 0 if d.passed else 1


if __name__ == "__main__":
    sys.exit(main())
