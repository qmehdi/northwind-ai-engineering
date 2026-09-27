"""Train, calibrate, threshold, evaluate, and save a versioned triage model.

    uv run python -m nw.triage.train --data data/tickets.jsonl --out artifacts/triage

The trap in this session is class imbalance. P0 is about 4 percent of tickets.
A classifier trained naively scores high accuracy and almost never predicts P0.
The fixes, in the order the guide walks through them: class weights, then a
P0 threshold chosen on the validation split, then probability calibration so
the threshold means what it says.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline

from nw.triage.features import PRIORITIES, build_features
from nw.triage.model import TriageModel


def load_tickets(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def by_split(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    for r in rows:
        out[r.get("split", "train")].append(r)
    return out


def labels(rows: list[dict[str, Any]]) -> np.ndarray:
    return np.asarray([r["priority"] for r in rows])


def build_pipeline(class_weight: str | dict | None = "balanced", C: float = 4.0) -> Pipeline:
    """Features, then a linear classifier. Linear is deliberate: it trains in seconds on a
    laptop, its coefficients are readable, and on short texts it is hard to beat by much."""
    clf = LogisticRegression(max_iter=2000, C=C)  # Step 3: what about the 4 percent?
    return Pipeline([("features", build_features()), ("clf", clf)])


def choose_p0_threshold(
    proba_p0: np.ndarray, y: np.ndarray, target_recall: float = 0.90, min_precision: float = 0.25
) -> float:
    """Lowest threshold that reaches the target P0 recall on the validation split while
    keeping precision above the floor; if no threshold does, the one with the best F1."""
    return 0.5  # Step 4: sweep thresholds on the validation split


def expected_calibration_error(proba_max: np.ndarray, correct: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (proba_max > lo) & (proba_max <= hi)
        if m.any():
            ece += m.mean() * abs(proba_max[m].mean() - correct[m].mean())
    return float(ece)


def evaluate(model: TriageModel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    y = labels(rows)
    proba = model.predict_proba(rows)
    pred = np.asarray([model.decide(p)[0] for p in proba])
    argmax = np.asarray([PRIORITIES[int(np.argmax(p))] for p in proba])
    is_p0 = y == "P0"
    return {
        "n": len(rows),
        "accuracy": float((pred == y).mean()),
        "macro_f1": float(f1_score(y, pred, average="macro", labels=PRIORITIES)),
        "macro_f1_argmax": float(f1_score(y, argmax, average="macro", labels=PRIORITIES)),
        "p0_recall": float(recall_score(is_p0, pred == "P0", zero_division=0)),
        "p0_precision": float(precision_score(is_p0, pred == "P0", zero_division=0)),
        "p0_recall_argmax": float(recall_score(is_p0, argmax == "P0", zero_division=0)),
        "confusion": confusion_matrix(y, pred, labels=PRIORITIES).tolist(),
        "brier_p0": float(brier_score_loss(is_p0, proba[:, 0])),
        "ece": expected_calibration_error(proba.max(axis=1), (argmax == y).astype(float)),
    }


def majority_baseline(rows: list[dict[str, Any]]) -> dict[str, float]:
    y = labels(rows)
    top = max(PRIORITIES, key=lambda p: (y == p).sum())
    pred = np.full_like(y, top)
    return {
        "predicts": top,
        "accuracy": float((pred == y).mean()),
        "macro_f1": float(f1_score(y, pred, average="macro", labels=PRIORITIES)),
        "p0_recall": 0.0,
    }


def data_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "nogit"


def train(
    data: Path,
    out: Path,
    *,
    class_weight: str | None = "balanced",
    calibrate: bool = True,
    target_recall: float = 0.90,
    min_precision: float = 0.25,
    seed: int = 0,
) -> tuple[TriageModel, dict[str, Any]]:
    rows = load_tickets(data)
    splits = by_split(rows)
    train_rows, val_rows, test_rows = splits["train"], splits["val"], splits["test"]
    pipeline = build_pipeline(class_weight=class_weight)
    pipeline.fit(train_rows, labels(train_rows))

    final, classes = pipeline, list(pipeline.named_steps["clf"].classes_)  # Step 5

    provisional = TriageModel(pipeline=final, classes=classes, p0_threshold=0.5, metadata={})
    val_proba = provisional.predict_proba(val_rows)
    threshold = choose_p0_threshold(
        val_proba[:, 0], labels(val_rows), target_recall=target_recall, min_precision=min_precision
    )

    version = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M')}-{git_sha()}-{data_hash(data)}"
    model = TriageModel(
        pipeline=final,
        classes=classes,
        p0_threshold=threshold,
        metadata={
            "version": version,
            "trained_at": dt.datetime.now(dt.UTC).isoformat(),
            "data": str(data),
            "data_sha256_12": data_hash(data),
            "git_sha": git_sha(),
            "n_train": len(train_rows),
            "n_val": len(val_rows),
            "n_test": len(test_rows),
            "class_weight": class_weight,
            "calibrated": calibrate,
            "target_p0_recall": target_recall,
            "min_p0_precision": min_precision,
            "seed": seed,
        },
    )
    report = {
        "baseline_majority": majority_baseline(test_rows),
        "val": evaluate(model, val_rows),
        "test": evaluate(model, test_rows),
        "p0_threshold": threshold,
    }
    model.metadata["metrics"] = report
    target = out / version
    model.save(target)
    (target / "report.json").write_text(json.dumps(report, indent=1))
    latest = out / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(version, target_is_directory=True)
    return model, report


def format_report(report: dict[str, Any]) -> str:
    t, b = report["test"], report["baseline_majority"]
    lines = [
        "| Metric | Majority baseline | Model |",
        "| --- | ---: | ---: |",
        f"| Accuracy | {b['accuracy']:.3f} | {t['accuracy']:.3f} |",
        f"| Macro-F1 | {b['macro_f1']:.3f} | {t['macro_f1']:.3f} |",
        f"| P0 recall | {b['p0_recall']:.3f} | {t['p0_recall']:.3f} |",
        f"| P0 precision | n/a | {t['p0_precision']:.3f} |",
        f"| P0 recall with plain argmax | n/a | {t['p0_recall_argmax']:.3f} |",
        f"| Brier (P0) | n/a | {t['brier_p0']:.4f} |",
        f"| ECE | n/a | {t['ece']:.4f} |",
        f"| P0 threshold | n/a | {report['p0_threshold']:.2f} |",
        "",
        "Confusion matrix on test (rows true, columns predicted, P0 to P3):",
        "",
    ]
    for label, row in zip(PRIORITIES, t["confusion"], strict=True):
        lines.append(f"    {label}  " + "  ".join(f"{v:5d}" for v in row))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage"))
    ap.add_argument("--no-class-weight", action="store_true")
    ap.add_argument("--no-calibration", action="store_true")
    ap.add_argument("--target-recall", type=float, default=0.90)
    ap.add_argument("--min-precision", type=float, default=0.25)
    args = ap.parse_args()
    model, report = train(
        args.data,
        args.out,
        class_weight=None if args.no_class_weight else "balanced",
        calibrate=not args.no_calibration,
        target_recall=args.target_recall,
        min_precision=args.min_precision,
    )
    print(f"model {model.version}\n")
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
