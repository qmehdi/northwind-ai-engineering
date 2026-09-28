"""A model card generated from what the artifact already knows, so it is never out of date.

uv run python -m nw.triage.model_card artifacts/triage/latest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def render(metadata: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
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
        + (", sigmoid calibration on the validation split" if metadata.get("calibrated") else ""),
        f"- P0 threshold {m['p0_threshold']:.2f}, chosen on validation for P0 recall "
        f"{metadata.get('target_p0_recall', 0.9):.2f} with precision at least "
        f"{metadata.get('min_p0_precision', 0.25):.2f}",
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
    by_lang = t.get("by_language") or {}
    if by_lang:
        lines += [
            "",
            "## Performance by language",
            "",
            "| Language | Rows | Macro-F1 | P0 recall |",
            "| --- | ---: | ---: | ---: |",
        ]
        for lang, r in sorted(by_lang.items()):
            p0 = "n/a" if r.get("p0_recall") is None else f"{r['p0_recall']:.3f}"
            lines.append(f"| {lang} | {r['n']} | {r['macro_f1']:.3f} | {p0} |")
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
        "- The service measures input and prediction drift against this training profile "
        "(`/drift`) and logs `drift_alert` past a PSI of 0.2; the deployment alarms on it.",
        "- Promotion goes through the gate in `nw/triage/promote.py`; the decision log is "
        "`artifacts/triage/promotions.jsonl`.",
        "",
    ]
    return "\n".join(lines)


def write(artifact_dir: Path) -> Path:
    metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    prof_path = artifact_dir / "data_profile.json"
    profile = json.loads(prof_path.read_text(encoding="utf-8")) if prof_path.exists() else None
    out = artifact_dir / "MODEL_CARD.md"
    out.write_text(render(metadata, profile), encoding="utf-8")
    return out


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/triage/latest")
    print(write(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
