"""A model card generated from what the artifact already knows, so it is never out of date.

    uv run python -m nw.semantic.model_card artifacts/semantic/latest

Training writes the first card; the export, the benchmark and the gate each add what
they measured when the card is regenerated, which promotion does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def render(
    metadata: dict[str, Any],
    profile: dict[str, Any] | None = None,
    thresholds: np.ndarray | None = None,
    export: dict[str, Any] | None = None,
    benchmark: list[dict[str, Any]] | None = None,
) -> str:
    m = metadata["metrics"]
    v, t = m["val"], m["test"]
    lora = metadata["lora"]
    share = 100 * metadata["parameters_trainable"] / max(metadata["parameters_total"], 1)
    lines = [
        f"# Model card: Northwind semantic engine {metadata['version']}",
        "",
        "## Intended use",
        "",
        "Reads a support ticket's subject and body and returns the tags it is about (several "
        "per ticket), a priority P0 to P3, and the embedding the similar-tickets index "
        "searches with. The tags route a ticket by topic; the priority is advisory. Project "
        "1's threshold rule carries Northwind's P0 SLA in front of this model, because this "
        "head decides by argmax and does not protect the rare class. Not for use on other "
        "products or languages the training data does not cover.",
        "",
        "## Model",
        "",
        f"- `{metadata['base']}` with LoRA adapters (rank {lora['r']}, alpha {lora['alpha']}, "
        f"dropout {lora.get('dropout', 0.05)}) on {', '.join(lora['targets'])}, two linear heads "
        "over the mean-pooled encoder output",
        f"- {metadata['parameters_trainable']:,} of {metadata['parameters_total']:,} parameters "
        f"trained ({share:.2f} percent); the base is frozen and folded back in at export",
        "- Multi-label tags: binary cross-entropy per tag, one decision threshold per tag "
        "chosen on the validation split for F1. Priority: weighted cross-entropy, argmax",
        f"- Trained {metadata['trained_at']} from `{metadata['data']}` "
        f"(sha {metadata['data_sha256_12']}), code {metadata['git_sha']}, seed {metadata['seed']}"
        + (f", resumed from `{metadata['resumed_from']}`" if metadata.get("resumed_from") else ""),
        f"- Rows: train {metadata['n_train']}"
        + (" (stratified subset of the training split)" if metadata.get("subset") else "")
        + f", validation {metadata['n_val']}, test {metadata['n_test']}; "
        f"{metadata['epochs']} epochs, learning rate {metadata.get('lr', 0):.0e}, batch "
        f"{metadata.get('batch_size', '?')} x {metadata.get('accumulate', '?')} accumulation, "
        f"max length {metadata['max_length']}; {metadata['seconds']:.0f} s on {metadata['device']}",
        "",
        "## Evaluation of the best epoch",
        "",
        "| Metric | Validation | Test |",
        "| --- | ---: | ---: |",
        f"| Tag micro-F1 | {v['tag_micro_f1']:.3f} | {t['tag_micro_f1']:.3f} |",
        f"| Tag macro-F1 | {v['tag_macro_f1']:.3f} | {t['tag_macro_f1']:.3f} |",
        f"| Priority macro-F1 | {v['priority_macro_f1']:.3f} | {t['priority_macro_f1']:.3f} |",
        f"| P0 recall | {v['p0_recall']:.3f} | {t['p0_recall']:.3f} |",
        "",
        "The best epoch and the tag thresholds were chosen on validation; the test column is "
        "the number to report. The validation split is also where the per-epoch history in "
        "`runs.jsonl` comes from.",
    ]
    by_lang = t.get("by_language") or {}
    if by_lang:
        lines += [
            "",
            "## Performance by language, test split",
            "",
            "| Language | Rows | Tag micro-F1 | Priority macro-F1 | P0 recall |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for lang, r in sorted(by_lang.items()):
            p0 = "n/a" if r.get("p0_recall") is None else f"{r['p0_recall']:.3f}"
            lines.append(
                f"| {lang} | {r['n']} | {r['tag_micro_f1']:.3f} | {r['priority_macro_f1']:.3f} "
                f"| {p0} |"
            )
    if thresholds is not None:
        tags = metadata["tags"]
        low = sorted(zip(tags, thresholds, strict=True), key=lambda x: x[1])
        lines += [
            "",
            "## Tag thresholds",
            "",
            f"One per tag, chosen on validation; {sum(1 for _, x in low if x != 0.5)} of "
            f"{len(tags)} differ from a flat 0.5, from {low[0][1]:.2f} ({low[0][0]}) to "
            f"{low[-1][1]:.2f} ({low[-1][0]}). The ten lowest, the rare tags a flat threshold "
            "would under-predict: " + ", ".join(f"{name} {x:.2f}" for name, x in low[:10]) + ".",
        ]
    if export:
        lines += [
            "",
            "## Export and serving",
            "",
            "- ONNX opset 17, dynamic batch and sequence axes; fp32 "
            f"{export['onnx_fp32_bytes'] / 1e6:.1f} MB, int8 {export['onnx_int8_bytes'] / 1e6:.1f} "
            f"MB (ratio {export['size_ratio']})",
            f"- Parity against PyTorch on held-out tickets, max absolute logit difference: fp32 "
            f"{export['max_abs_diff_fp32']:.2e}, int8 {export['max_abs_diff_int8']:.2f}",
            "- The service runs the int8 graph on ONNX Runtime by default (`NW_QUANTIZED=1`); "
            "the tokenizer ships in the artifact so the image never reaches the Hub",
        ]
    if benchmark:
        lines += [
            "",
            "## Benchmark against Project 1, test split, batch size one",
            "",
            "| Model | Priority macro-F1 | P0 recall | Tag micro-F1 | p50 ms | p95 ms | Size MB |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in benchmark:
            tag = "n/a" if r["tag_micro_f1"] is None else f"{r['tag_micro_f1']:.3f}"
            lines.append(
                f"| {r['model']} | {r['priority_macro_f1']:.3f} | {r['p0_recall']:.3f} | {tag} "
                f"| {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['bytes'] / 1e6:.1f} |"
            )
    if profile:
        top = sorted(profile.get("tag_share", {}).items(), key=lambda x: -x[1])[:10]
        lines += [
            "",
            "## Training data",
            "",
            "Priority share: "
            + ", ".join(f"{k} {x:.1%}" for k, x in profile["priority_share"].items()),
            "",
            "Language share: "
            + ", ".join(f"{k} {x:.1%}" for k, x in profile["language_share"].items()),
            "",
            "Text length (characters) p10 to p90: "
            + " ".join(f"{x:.0f}" for x in profile["text_length_quantiles"].values()),
        ]
        if top:
            lines += [
                "",
                "Most frequent tags in the training rows: "
                + ", ".join(f"{k} {x:.1%}" for k, x in top),
            ]
        lines += [
            "",
            "The data is synthetic, generated for the course; no real customer text was used.",
        ]
    lines += [
        "",
        "## Limitations and monitoring",
        "",
        "- Priority is argmax over four classes; P0 recall is below Project 1's threshold rule "
        "by design, so this model is not the one that decides an SLA.",
        "- The base model is English; the languages with few training rows are reported above "
        "and are expected to be worse.",
        "- Dynamic int8 quantisation costs the rare class first; the gate bounds the loss "
        "against fp32 and the benchmark shows it.",
        "- The service measures drift against this training profile (`/drift`): text length, "
        "predicted priority and the tag rate, and logs `drift_alert` past a PSI of 0.2; "
        "the deployment alarms on it.",
        "- Promotion goes through the gate in `nw/semantic/promote.py`; the decision log is "
        "`artifacts/semantic/promotions.jsonl`.",
        "",
    ]
    return "\n".join(lines)


def write(artifact_dir: Path) -> Path:
    artifact_dir = Path(artifact_dir)
    metadata = _load(artifact_dir / "metadata.json")
    thresholds = (
        np.load(artifact_dir / "tag_thresholds.npy")
        if (artifact_dir / "tag_thresholds.npy").exists()
        else None
    )
    out = artifact_dir / "MODEL_CARD.md"
    out.write_text(
        render(
            metadata,
            _load(artifact_dir / "data_profile.json"),
            thresholds,
            _load(artifact_dir / "export_report.json"),
            _load(artifact_dir / "benchmark.json"),
        ),
        encoding="utf-8",
    )
    return out


def main() -> int:
    import argparse

    from nw.semantic.artifacts import resolve

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "artifact",
        nargs="?",
        default="artifacts/semantic",
        help="a version directory, or the root, which means latest",
    )
    args = ap.parse_args()
    print(write(resolve(args.artifact, serve=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
