"""The promotion gate: a candidate becomes the served model only by passing it.

    uv run python -m nw.triage.promote                       # newest candidate against production
    uv run python -m nw.triage.promote --candidate <version>
    uv run python -m nw.triage.promote --candidate <version> --force --reason "..."   # recorded

Absolute bars protect Northwind (P0 recall, calibration, and P0 recall per language on the
held-out gate set); relative bars protect against regressions (macro-F1, Brier and missed P0
tickets against the model currently in production). The comparison is only valid on the same
test split, so a data change is a finding, not a silent pass.

Small samples are stated, not hidden. The test split holds 31 P0 tickets: 28 of 31 is 0.903
with a 95 percent interval of 0.75 to 0.97, and a drop of 0.03 is less than one ticket. So the
P0 regression bar is counted in tickets (`max_p0_missed_increase`), paired on the same rows
when both summaries carry their predictions, with McNemar's exact test and a paired bootstrap
of the macro-F1 difference printed as evidence. A language needs `min_slice_p0` P0 tickets in
the gate set before its bar applies; below that the decision says `insufficient evidence`
instead of pretending. A slice below its bar fails the gate unless the policy carries a
written waiver, which every decision and the model card then show.

A passed gate points `artifacts/triage/latest` at the candidate, moves the registry alias, and
writes the production summary the retraining workflow compares against in CI, where no
artifact directory exists. Every decision records who promoted and why (`--by`, else
`NW_ACTOR`, `GITHUB_ACTOR` or the login name); a forced promotion needs a reason.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nw.evalstats import as_count, fmt_rate, paired_counts, proportion
from nw.logging import get_logger

log = get_logger("nw.triage.promote")
PRODUCTION_SUMMARY = Path("data/golden/triage_production.json")
PRIORITIES = ("P0", "P1", "P2", "P3")

# A waiver is a decision a person made and wrote down, not a lowered bar: the slice still
# fails its bar, the decision says so, and the model card prints the text.
DEFAULT_WAIVERS: dict[str, str] = {
    "de": (
        "German P0 recall is a known limitation of this English-heavy TF-IDF model (314 German "
        "training rows, 13 of them P0). Accepted by the course author on 2026-09-30 for "
        "teaching, to be reviewed by 2026-12-31; lifting it is the reference tab's exercise."
    ),
}


def actor() -> str:
    """Who is promoting: NW_ACTOR, else GITHUB_ACTOR (CI), else the login name."""
    for var in ("NW_ACTOR", "GITHUB_ACTOR"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class GatePolicy:
    min_p0_recall: float = 0.85
    max_ece: float = 0.12
    max_macro_f1_drop: float = 0.02
    max_brier_increase: float = 0.01
    # Retired 2026-09-30: a rate tolerance under one ticket. Kept so pipeline parameters
    # written before then still parse; `max_p0_missed_increase` decides.
    max_p0_recall_drop: float = 0.03
    max_p0_missed_increase: int = 1  # P0 tickets, net, on the same test rows
    min_slice_p0: int = 10  # P0 tickets a language needs in the gate set before its bar applies
    min_slice_p0_recall: float = 0.70
    slice_waivers: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_WAIVERS))


@dataclass
class Decision:
    candidate: str
    production: str | None
    passed: bool
    reasons: list[str] = field(default_factory=list)
    forced: bool = False
    decided_at: str = ""
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    insufficient_evidence: list[str] = field(default_factory=list)
    waived: dict[str, str] = field(default_factory=dict)
    decided_by: str = ""
    reason: str = ""


def summary(metadata: dict[str, Any]) -> dict[str, Any]:
    """What the gate compares and what CI keeps: the test metrics, the P0 counts behind the
    recall, the per-row test predictions for a paired comparison, and the gate set per
    language when training measured one."""
    m = metadata["metrics"]
    t = m["test"]
    out: dict[str, Any] = {
        "version": metadata["version"],
        "data_sha256_12": metadata["data_sha256_12"],
        "test": {k: t[k] for k in ("macro_f1", "p0_recall", "p0_precision", "ece", "brier_p0")},
        "p0_threshold": m["p0_threshold"],
    }
    if t.get("p0_recall_ci"):
        out["test_counts"] = {"p0_tp": t["p0_recall_ci"]["k"], "n_p0": t["p0_recall_ci"]["n"]}
    if t.get("predictions"):
        out["test_predictions"] = t["predictions"]
    if m.get("threshold"):
        out["threshold"] = {k: m["threshold"][k] for k in ("target_met", "target_recall")}
    if m.get("gate_set"):
        g = m["gate_set"]
        out["gate_set"] = {
            "source": g.get("source"),
            "slices_sha256_12": g.get("slices_sha256_12"),
            "by_language": {
                lang: {"n": r["n"], "n_p0": r.get("n_p0"), "p0_tp": r["p0_recall_ci"]["k"]}
                for lang, r in g["by_language"].items()
                if r.get("p0_recall_ci")
            },
        }
    return out


def _p0_counts(s: dict[str, Any]) -> tuple[int, int]:
    counts = s.get("test_counts")
    if counts:
        return int(counts["p0_tp"]), int(counts["n_p0"])
    return as_count(s["test"]["p0_recall"])


def _paired(candidate: dict[str, Any], production: dict[str, Any]) -> dict[str, Any] | None:
    """McNemar on P0 hits and a paired bootstrap of macro-F1, when both summaries carry
    predictions for the same ordered test rows. None when they cannot be paired."""
    a, b = production.get("test_predictions"), candidate.get("test_predictions")
    if (
        not a
        or not b
        or a["ids_sha256_12"] != b["ids_sha256_12"]
        or len(a["pred"]) != len(b["pred"])
    ):
        return None
    truth = b.get("truth") or a.get("truth")
    if not truth or len(truth) != len(b["pred"]):
        return None
    y = [PRIORITIES[int(ch)] for ch in truth]
    pa = [PRIORITIES[int(ch)] for ch in a["pred"]]
    pb = [PRIORITIES[int(ch)] for ch in b["pred"]]
    p0 = [i for i, v in enumerate(y) if v == "P0"]
    hits = paired_counts([pa[i] == "P0" for i in p0], [pb[i] == "P0" for i in p0])

    def macro_f1(rows: list[int], pred: list[str]) -> float:
        f1s = []
        for cls in PRIORITIES:
            tp = sum(1 for i in rows if pred[i] == cls and y[i] == cls)
            fp = sum(1 for i in rows if pred[i] == cls and y[i] != cls)
            fn = sum(1 for i in rows if pred[i] != cls and y[i] == cls)
            f1s.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
        return sum(f1s) / len(f1s)

    # Paired bootstrap over rows: both models are scored on the same resampled rows.
    rng = random.Random(0)
    idx = list(range(len(y)))
    diffs = sorted(
        macro_f1(rows, pb) - macro_f1(rows, pa)
        for rows in ([rng.randrange(len(idx)) for _ in idx] for _ in range(1000))
    )
    return {
        "p0_hits": hits,
        "macro_f1_diff": macro_f1(idx, pb) - macro_f1(idx, pa),
        "macro_f1_diff_ci": (diffs[25], diffs[974]),
    }


def gate(
    candidate: dict[str, Any], production: dict[str, Any] | None, policy: GatePolicy | None = None
) -> Decision:
    """Compare two production summaries (see `summary`); None means the first model."""
    policy = policy or GatePolicy()
    c = candidate["test"]
    reasons: list[str] = []
    notes: list[str] = []
    insufficient: list[str] = []
    waived: dict[str, str] = {}
    k, n = _p0_counts(candidate)
    evidence: dict[str, Any] = {"p0_recall": proportion(k, n)}
    if c["p0_recall"] < policy.min_p0_recall:
        reasons.append(
            f"P0 recall {fmt_rate(evidence['p0_recall'])} is below the bar "
            f"{policy.min_p0_recall:.2f}"
        )
    if c["ece"] > policy.max_ece:
        reasons.append(f"ECE {c['ece']:.3f} is above the bar {policy.max_ece:.2f}")
    thr = candidate.get("threshold")
    if thr and not thr.get("target_met", True):
        notes.append(
            f"the threshold missed its P0 recall target {thr.get('target_recall')} on the "
            "threshold half of validation and fell back to best F1"
        )
    gate_set = (candidate.get("gate_set") or {}).get("by_language") or {}
    evidence["slices"] = {}
    for lang, r in sorted(gate_set.items()):
        rate = proportion(int(r["p0_tp"]), int(r.get("n_p0") or 0))
        evidence["slices"][lang] = rate
        if rate["n"] < policy.min_slice_p0:
            insufficient.append(
                f"{lang}: {rate['n']} P0 tickets in the gate set, fewer than {policy.min_slice_p0}"
            )
            continue
        if rate["rate"] < policy.min_slice_p0_recall:
            text = (
                f"{lang} P0 recall on the gate set {fmt_rate(rate)} is below the slice bar "
                f"{policy.min_slice_p0_recall:.2f}"
            )
            if lang in policy.slice_waivers:
                waived[lang] = policy.slice_waivers[lang]
                notes.append(f"WAIVED {text}: {policy.slice_waivers[lang]}")
            else:
                reasons.append(text)
    if not gate_set:
        insufficient.append("no gate set: per-language bars not applied")
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
        pk, pn = _p0_counts(production)
        paired = _paired(candidate, production)
        if paired:
            evidence["paired"] = paired
            h = paired["p0_hits"]
            extra = h["champion_only"] - h["challenger_only"]
            lo, hi = paired["macro_f1_diff_ci"]
            notes.append(
                f"paired on {h['n']} test P0 tickets: production alone caught "
                f"{h['champion_only']}, the candidate alone {h['challenger_only']} (McNemar "
                f"p={h['mcnemar_p']:.3f}); macro-F1 difference {paired['macro_f1_diff']:+.3f}, "
                f"95% CI {lo:+.3f} to {hi:+.3f}"
            )
        else:
            extra = pk - k if pn == n else round((pk / pn - k / n) * n) if pn and n else 0
            notes.append(
                "the two summaries cannot be paired row by row (no predictions for the same "
                "rows): missed P0 tickets compared as counts"
            )
        if extra > policy.max_p0_missed_increase:
            reasons.append(
                f"the candidate misses {extra} more P0 tickets than production ({k}/{n} against "
                f"{pk}/{pn}); the bar allows {policy.max_p0_missed_increase}"
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
        notes=notes,
        evidence=evidence,
        insufficient_evidence=insufficient,
        waived=waived,
    )


recheck = gate  # an approval in a registry calls this again on the stored summaries


def read_metadata(artifact_dir: Path) -> dict[str, Any]:
    return json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))


def current_production(out: Path, summary_path: Path = PRODUCTION_SUMMARY) -> dict[str, Any] | None:
    """The served model if the artifact tree has one, else the committed summary, else None."""
    latest = out / "latest"
    if (latest / "metadata.json").exists():
        return summary(read_metadata(latest))
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
    """Run the gate and, when it passes, move `latest`. Who decided and why travel with
    the decision; a forced promotion without a reason is refused."""
    if force and not reason.strip():
        raise SystemExit("--force needs --reason: an override nobody explained is not an override")
    cand_dir = out / candidate_version
    cand = summary(read_metadata(cand_dir))
    prod = current_production(out, summary_path)
    if prod and prod["version"] == cand["version"]:
        prod = None  # promoting the served model again is a no-op comparison
    decision = gate(cand, prod, policy)
    decision.decided_by = by or actor()
    decision.reason = reason
    if force and not decision.passed:
        decision.passed, decision.forced = True, True
    (out / "promotions.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with (out / "promotions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(decision)) + "\n")
    # The latest decision beside the artifact, so the card can show waivers and evidence.
    (cand_dir / "promotion.json").write_text(json.dumps(asdict(decision), indent=1))
    from nw.triage.model_card import write as write_card

    write_card(cand_dir)
    if decision.passed:
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
    lines = [head, *(f"  {r}" for r in d.reasons)]
    p0 = (d.evidence or {}).get("p0_recall")
    if p0:
        lines.append(f"  evidence: test P0 recall {fmt_rate(p0)}")
    lines += [f"  note: {n}" for n in d.notes]
    lines += [f"  insufficient evidence: {x}" for x in d.insufficient_evidence]
    if d.decided_by:
        lines.append(f"  decided by {d.decided_by}" + (f": {d.reason}" if d.reason else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
    ap.add_argument("--candidate", default=None, help="artifact version; default is the newest")
    ap.add_argument(
        "--force", action="store_true", help="promote despite a failed gate, recorded as forced"
    )
    ap.add_argument("--summary", type=Path, default=PRODUCTION_SUMMARY)
    ap.add_argument("--by", default=None, help="who promotes; default NW_ACTOR or the login")
    ap.add_argument("--reason", default="", help="why; required with --force")
    args = ap.parse_args()
    version = args.candidate or newest_candidate(args.out)
    d = promote(
        args.out,
        version,
        force=args.force,
        summary_path=args.summary,
        by=args.by,
        reason=args.reason,
    )
    print(format_decision(d))
    return 0 if d.passed else 1


if __name__ == "__main__":
    sys.exit(main())
