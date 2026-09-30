"""A model card generated from what the artifact already knows, so it is never out of date.

uv run python -m nw.triage.model_card artifacts/triage/latest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nw.evalstats import fmt_rate


def _p0_cell(r: dict[str, Any]) -> str:
    ci = r.get("p0_recall_ci")
    if ci and ci.get("n"):
        return fmt_rate(ci)
    return "n/a" if r.get("p0_recall") is None else f"{r['p0_recall']:.3f}"


def render(
    metadata: dict[str, Any],
    profile: dict[str, Any] | None = None,
    promotion: dict[str, Any] | None = None,
) -> str:
    m = metadata["metrics"]
    t, v, b = m["test"], m["val"], m["baseline_majority"]
    lines = [
        f"# Model card: Northwind ticket triage {metadata['version']}",
        "",
        "## Intended use",
        "",
        "Scores a support ticket's subject and body into a priority P0 to P3 for the "
        "routing step in front of the resolution agent. The decision rule favours P0 "
        "recall: a P0 is predicted whenever its calibrated probability clears the "
        "threshold below, because a missed outage costs more than a false alarm. Not "
        "for use on other products or languages the training data does not cover.",
        "",
        "## Model",
        "",
        "- TF-IDF over word and character n-grams, logistic regression"
        + (" with balanced class weights" if metadata.get("class_weight") else "")
        + (
            ", sigmoid calibration on the calibration half of validation"
            if metadata.get("calibrated")
            else ""
        ),
        f"- P0 threshold {m['p0_threshold']:.2f}, the highest on a grid from 0.95 down to 0.01 "
        f"that reached P0 recall {metadata.get('target_p0_recall', 0.9):.2f} with precision at "
        f"least {metadata.get('min_p0_precision', 0.25):.2f} on the threshold half of "
        "validation"
        + (
            ""
            if (m.get("threshold") or {}).get("target_met", True)
            else " (TARGET NOT MET: the best-F1 threshold was used instead)"
        ),
        f"- Trained {metadata['trained_at']} from `{metadata['data']}` "
        f"(sha {metadata['data_sha256_12']}), code {metadata['git_sha']}, "
        f"seed {metadata.get('seed', 0)}",
        f"- Rows: train {metadata['n_train']}, validation {metadata['n_val']}, "
        f"test {metadata['n_test']}",
        "",
        "## Evaluation on the held-out test split",
        "",
        "| Metric | Majority baseline | Model |",
        "| --- | ---: | ---: |",
        f"| Accuracy | {b['accuracy']:.3f} | {t['accuracy']:.3f} |",
        f"| Macro-F1 | {b['macro_f1']:.3f} | {t['macro_f1']:.3f} |",
        f"| P0 recall | {b['p0_recall']:.3f} | {t['p0_recall']:.3f} |",
        f"| P0 precision | n/a | {t['p0_precision']:.3f} |",
        f"| Brier (P0) | n/a | {t['brier_p0']:.4f} |",
        f"| Expected calibration error | n/a | {t['ece']:.4f} |",
        "",
        f"Validation, for reference: macro-F1 {v['macro_f1']:.3f}, P0 recall {v['p0_recall']:.3f}.",
    ]
    if t.get("p0_recall_ci"):
        lines += [
            "",
            f"P0 recall on test is {fmt_rate(t['p0_recall_ci'])}; P0 precision "
            f"{fmt_rate(t['p0_precision_ci'])}. The intervals are 95 percent Wilson intervals: "
            "with this few P0 tickets, two models whose point estimates differ by one ticket "
            "cannot be told apart, which is why the gate counts missed tickets instead.",
        ]
    by_lang = t.get("by_language") or {}
    if by_lang:
        lines += [
            "",
            "## Performance by language",
            "",
            "Test split:",
            "",
            "| Language | Rows | P0 rows | Macro-F1 | P0 recall (95% CI) |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for lang, r in sorted(by_lang.items()):
            lines.append(
                f"| {lang} | {r['n']} | {r.get('n_p0', 'n/a')} | {r['macro_f1']:.3f} | "
                f"{_p0_cell(r)} |"
            )
    gate_set = m.get("gate_set")
    if gate_set:
        lines += [
            "",
            f"Gate set ({gate_set.get('source', 'test split plus golden slices')}), the rows the "
            "per-language bars read:",
            "",
            "| Language | Rows | P0 rows | Macro-F1 | P0 recall (95% CI) |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for lang, r in sorted(gate_set["by_language"].items()):
            lines.append(
                f"| {lang} | {r['n']} | {r.get('n_p0', 'n/a')} | {r['macro_f1']:.3f} | "
                f"{_p0_cell(r)} |"
            )
    if promotion:
        if promotion.get("waived"):
            lines += ["", "Waived slice bars, each a written decision, not a lowered bar:", ""]
            lines += [f"- {lang}: {text}" for lang, text in promotion["waived"].items()]
        if promotion.get("insufficient_evidence"):
            lines += ["", "Insufficient evidence, bars not applied:", ""]
            lines += [f"- {x}" for x in promotion["insufficient_evidence"]]
    if profile:
        lines += [
            "",
            "## Training data",
            "",
            "Priority share: "
            + ", ".join(f"{k} {v:.1%}" for k, v in profile["priority_share"].items()),
            "",
            "Language share: "
            + ", ".join(f"{k} {v:.1%}" for k, v in profile["language_share"].items()),
            "",
            "Text length (characters) p10 to p90: "
            + " ".join(f"{v:.0f}" for v in profile["text_length_quantiles"].values()),
            "",
            "The data is synthetic, generated for the course; no real customer text was used.",
        ]
    lines += [
        "",
        "## Limitations and monitoring",
        "",
        "- Short or empty bodies carry little signal; the threshold rule then decides on "
        "the subject alone.",
        "- Performance on languages with few training rows is reported above and is "
        "expected to be lower.",
        "- The service measures input drift against this training profile and prediction "
        "drift against the validation predictions (`/drift`), waits for 200 requests, and logs "
        "`drift_alert` past a PSI of 0.2 or the chance level of the window, whichever is "
        "higher; the deployment alarms on it. The canary also watches the P0 share and the "
        "shadow agreement (`nw_triage_quality_level`).",
        "- Promotion goes through the gate in `nw/triage/promote.py`; the decision log is "
        "`artifacts/triage/promotions.jsonl`.",
        "",
    ]
    return "\n".join(lines)


def write(artifact_dir: Path) -> Path:
    metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    prof_path = artifact_dir / "data_profile.json"
    profile = json.loads(prof_path.read_text(encoding="utf-8")) if prof_path.exists() else None
    promo_path = artifact_dir / "promotion.json"
    promotion = json.loads(promo_path.read_text(encoding="utf-8")) if promo_path.exists() else None
    out = artifact_dir / "MODEL_CARD.md"
    out.write_text(render(metadata, profile, promotion), encoding="utf-8")
    return out


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/triage/latest")
    print(write(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
