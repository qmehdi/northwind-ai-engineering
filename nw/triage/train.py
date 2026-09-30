"""Train, calibrate, threshold, evaluate, and save a versioned triage model.

    uv run python -m nw.triage.train --data data/tickets.jsonl --out artifacts/triage

The trap in this session is class imbalance. P0 is about 4 percent of tickets.
A classifier trained naively scores high accuracy and almost never predicts P0.
The fixes, in the order the guide walks through them: class weights, then a
P0 threshold chosen on held-out data, then probability calibration so the
threshold means what it says.

The validation split is cut in two by ticket id, stratified by priority: the calibration
half fits the sigmoid, the threshold half chooses the P0 threshold. One split doing both
jobs would tune the threshold to the calibrator's own fit. The test split is only ever
measured. `data/golden/triage_slices.jsonl` adds a held-out gate set of P0 and German
tickets, so the per-language bars in the gate have enough P0 tickets to mean something.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline

from nw.evalstats import fmt_rate, proportion
from nw.triage.data_check import profile as profile_data
from nw.triage.data_check import validate
from nw.triage.features import PRIORITIES, build_features
from nw.triage.model import TriageModel

GATE_SLICES = Path("data/golden/triage_slices.jsonl")
# The slices were written for this dataset (same generator, labels and splits); a model
# trained on other data is not measured on them.
SLICES_FOR_DATA = "308ad1151d5c"
# Candidate thresholds, highest first: 0.95 down to 0.01. The floor sits below 0.05 because
# a calibrated P0 probability for a real outage can be that low on a four percent class.
THRESHOLD_GRID: tuple[float, ...] = tuple(round(0.95 - i * 0.01, 2) for i in range(95))


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
    # SOLUTION BEGIN
    clf = LogisticRegression(max_iter=2000, C=C, class_weight=class_weight)
    # STUB: clf = LogisticRegression(max_iter=2000, C=C)  # Class weights: what about the 4 percent?
    # SOLUTION END
    return Pipeline([("features", build_features()), ("clf", clf)])


def choose_p0_threshold(
    proba_p0: np.ndarray, y: np.ndarray, target_recall: float = 0.90, min_precision: float = 0.25
) -> float:
    """Highest threshold that reaches the target P0 recall on held-out data while keeping
    precision above the floor, scanning `THRESHOLD_GRID` from 0.95 down to 0.01; if no
    threshold does both, the one with the best F1. The highest passing threshold is the one
    with the fewest false alarms at the recall the business asked for."""
    # SOLUTION BEGIN
    is_p0 = y == "P0"
    best_t, best_f1 = 0.5, -1.0
    for t in THRESHOLD_GRID:
        pred = proba_p0 >= t
        if pred.sum() == 0:
            continue
        rec = recall_score(is_p0, pred, zero_division=0)
        prec = precision_score(is_p0, pred, zero_division=0)
        f1 = f1_score(is_p0, pred, zero_division=0)
        if rec >= target_recall and prec >= min_precision:
            return float(t)
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t
    # STUB: return 0.5  # threshold on its own half of validation
    # SOLUTION END


def threshold_report(
    proba_p0: np.ndarray,
    y: np.ndarray,
    threshold: float,
    target_recall: float = 0.90,
    min_precision: float = 0.25,
) -> dict[str, Any]:
    """What the chosen threshold achieves on the rows it was chosen on, and whether it met
    the target. A fallback to best F1 is recorded as `target_met: false`, never silent."""
    is_p0 = y == "P0"
    pred = proba_p0 >= threshold
    tp = int((pred & is_p0).sum())
    recall = proportion(tp, int(is_p0.sum()))
    precision = proportion(tp, int(pred.sum()))
    met = (recall["rate"] or 0.0) >= target_recall and (precision["rate"] or 0.0) >= min_precision
    return {
        "threshold": float(threshold),
        "target_recall": target_recall,
        "min_precision": min_precision,
        "target_met": bool(met),
        "recall": recall,
        "precision": precision,
        "rows": len(y),
    }


def calibration_split(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validation cut in two halves, stratified by priority, ordered by a hash of the ticket
    id so the cut is stable across runs and machines: (calibration, threshold)."""
    cal: list[dict[str, Any]] = []
    thr: list[dict[str, Any]] = []
    for p in PRIORITIES:
        group = sorted(
            (r for r in rows if r.get("priority") == p),
            key=lambda r: hashlib.sha256(
                str(r.get("ticket_id", r.get("body", ""))).encode()
            ).hexdigest(),
        )
        cal += group[0::2]
        thr += group[1::2]
    return cal, thr


def expected_calibration_error(proba_max: np.ndarray, correct: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (proba_max > lo) & (proba_max <= hi)
        if m.any():
            ece += m.mean() * abs(proba_max[m].mean() - correct[m].mean())
    return float(ece)


def evaluate(
    model: TriageModel,
    rows: list[dict[str, Any]],
    *,
    by_language: bool = True,
    predictions: bool = False,
) -> dict[str, Any]:
    """Point metrics, the P0 counts behind them with 95 percent Wilson intervals, and per
    language the same. With `predictions`, the per-row predicted priorities in row order and
    a hash of the row ids: what a paired test against another model on the same rows needs."""
    y = labels(rows)
    proba = model.predict_proba(rows)
    pred = np.asarray([model.decide(p)[0] for p in proba])
    argmax = np.asarray([PRIORITIES[int(np.argmax(p))] for p in proba])
    is_p0 = y == "P0"
    langs: dict[str, Any] = {}
    if by_language and any("language" in r for r in rows):
        lang = np.asarray([r.get("language", "en") for r in rows])
        for code in sorted(set(lang)):
            m = lang == code
            tp = int(((pred[m] == "P0") & is_p0[m]).sum())
            langs[code] = {
                "n": int(m.sum()),
                "n_p0": int(is_p0[m].sum()),
                "macro_f1": float(
                    f1_score(y[m], pred[m], average="macro", labels=PRIORITIES, zero_division=0)
                ),
                "p0_recall": float(recall_score(is_p0[m], pred[m] == "P0", zero_division=0))
                if is_p0[m].any()
                else None,
                "p0_recall_ci": proportion(tp, int(is_p0[m].sum())),
            }
    tp = int(((pred == "P0") & is_p0).sum())
    out: dict[str, Any] = {
        "by_language": langs,
        "n": len(rows),
        "n_p0": int(is_p0.sum()),
        "accuracy": float((pred == y).mean()),
        "macro_f1": float(f1_score(y, pred, average="macro", labels=PRIORITIES)),
        "macro_f1_argmax": float(f1_score(y, argmax, average="macro", labels=PRIORITIES)),
        "p0_recall": float(recall_score(is_p0, pred == "P0", zero_division=0)),
        "p0_precision": float(precision_score(is_p0, pred == "P0", zero_division=0)),
        "p0_recall_ci": proportion(tp, int(is_p0.sum())),
        "p0_precision_ci": proportion(tp, int((pred == "P0").sum())),
        "p0_recall_argmax": float(recall_score(is_p0, argmax == "P0", zero_division=0)),
        "confusion": confusion_matrix(y, pred, labels=PRIORITIES).tolist(),
        "brier_p0": float(brier_score_loss(is_p0, proba[:, 0])),
        "ece": expected_calibration_error(proba.max(axis=1), (argmax == y).astype(float)),
    }
    if predictions:
        out["predictions"] = {
            "ids_sha256_12": ids_sha(rows),
            "pred": "".join(str(PRIORITIES.index(p)) for p in pred),
            "truth": "".join(str(PRIORITIES.index(p)) for p in y),
        }
    return out


def ids_sha(rows: list[dict[str, Any]]) -> str:
    """Twelve hex characters over the ordered row ids: two prediction strings are paired
    only when this matches."""
    ids = "\n".join(str(r.get("ticket_id", i)) for i, r in enumerate(rows))
    return hashlib.sha256(ids.encode()).hexdigest()[:12]


def load_gate_slices(data: Path, path: Path | str | None = "auto") -> list[dict[str, Any]]:
    """The golden gate slices for `data`. `auto` uses `GATE_SLICES` when it exists and `data`
    is the dataset the slices were written for; a path forces it; None turns it off."""
    if path is None:
        return []
    if path == "auto":
        if not GATE_SLICES.exists() or data_hash(data) != SLICES_FOR_DATA:
            return []
        path = GATE_SLICES
    return load_tickets(Path(path))


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
    """The commit of the code that is running: NW_GIT_SHA, GITHUB_SHA, git, then the image
    label (`nw.platform.lineage`), so a container without `.git` does not record `nogit`."""
    from nw.platform.lineage import git_sha as lineage_sha

    return lineage_sha()


def train(
    data: Path,
    out: Path,
    *,
    class_weight: str | None = "balanced",
    calibrate: bool = True,
    target_recall: float = 0.90,
    min_precision: float = 0.25,
    seed: int = 0,
    promote: bool = True,
    gate_slices: Path | str | None = "auto",
) -> tuple[TriageModel, dict[str, Any]]:
    """Validate, train, evaluate, record the run, write the model card, then run the
    promotion gate. With `promote=False` the run is a registered candidate only."""
    rows = load_tickets(data)
    findings = validate(rows)
    blocking = [f for f in findings if f.blocking]
    if blocking:
        raise SystemExit(
            "data check failed: " + "; ".join(f"{f.check}: {f.detail}" for f in blocking)
        )
    data_profile = profile_data(rows, data_hash(data), findings)
    splits = by_split(rows)
    train_rows, val_rows, test_rows = splits["train"], splits["val"], splits["test"]
    cal_rows, thr_rows = calibration_split(val_rows)
    pipeline = build_pipeline(class_weight=class_weight)
    pipeline.fit(train_rows, labels(train_rows))

    # SOLUTION BEGIN
    if calibrate:
        # Calibrate on the calibration half of validation with the pipeline frozen: the classifier's
        # scores are mapped to probabilities that mean what they say. Isotonic needs
        # more data than sigmoid; sigmoid is the safe default at this size.
        features = pipeline.named_steps["features"]
        clf = pipeline.named_steps["clf"]
        calibrated = CalibratedClassifierCV(FrozenEstimator(clf), method="sigmoid")
        calibrated.fit(features.transform(cal_rows), labels(cal_rows))
        final = Pipeline([("features", features), ("clf", calibrated)])
        classes = list(calibrated.classes_)
    else:
        final = pipeline
        classes = list(pipeline.named_steps["clf"].classes_)
    # STUB: final, classes = pipeline, list(pipeline.named_steps["clf"].classes_)  # calibrate
    # SOLUTION END

    provisional = TriageModel(pipeline=final, classes=classes, p0_threshold=0.5, metadata={})
    thr_proba = provisional.predict_proba(thr_rows)
    threshold = choose_p0_threshold(
        thr_proba[:, 0], labels(thr_rows), target_recall=target_recall, min_precision=min_precision
    )
    chosen = threshold_report(
        thr_proba[:, 0], labels(thr_rows), threshold, target_recall, min_precision
    )

    version = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%d%H%M%S')}-{git_sha()}-{data_hash(data)}"
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
            "n_calibration": len(cal_rows),
            "n_threshold": len(thr_rows),
            "n_test": len(test_rows),
            "class_weight": class_weight,
            "calibrated": calibrate,
            "target_p0_recall": target_recall,
            "min_p0_precision": min_precision,
            "seed": seed,
        },
    )
    slices = load_gate_slices(data, gate_slices)
    report = {
        "baseline_majority": majority_baseline(test_rows),
        "val": evaluate(model, val_rows),
        "test": evaluate(model, test_rows, predictions=True),
        "p0_threshold": threshold,
        "threshold": chosen,
    }
    if slices:
        # The held-out gate set: the test split plus the golden slices, never trained,
        # calibrated or thresholded on. The per-language bars read this block.
        report["gate_set"] = evaluate(model, test_rows + slices) | {
            "source": f"test split + {GATE_SLICES}",
            "slices_sha256_12": ids_sha(slices),
        }
    model.metadata["metrics"] = report
    target = out / version
    model.save(target)
    (target / "report.json").write_text(json.dumps(report, indent=1))
    # The drift baseline for predictions is what the model predicts on held-out data, not
    # the label shares: a threshold rule predicts P0 more often than P0 occurs, by design.
    val_pred = [model.decide(p)[0] for p in model.predict_proba(val_rows)]
    profile_out = asdict(data_profile) | {
        "predicted_share": {p: val_pred.count(p) / max(len(val_pred), 1) for p in PRIORITIES},
        "predicted_share_source": f"validation predictions of {version}",
    }
    (target / "data_profile.json").write_text(json.dumps(profile_out, indent=1))
    from nw.triage.model_card import write as write_card
    from nw.triage.tracking import record_run

    write_card(target)
    report["run"] = record_run(out, model.metadata, report)
    if promote:
        from nw.triage.promote import promote as run_gate

        decision = run_gate(out, version, write_summary=data == Path("data/tickets.jsonl"))
        report["promotion"] = asdict(decision)
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
        f"P0 recall on test {fmt_rate(t['p0_recall_ci'])}; "
        f"precision {fmt_rate(t['p0_precision_ci'])}.",
    ]
    chosen = report.get("threshold")
    if chosen:
        lines.append(
            f"Threshold {chosen['threshold']:.2f} chosen on {chosen['rows']} threshold-half rows: "
            f"recall {fmt_rate(chosen['recall'])}, target {chosen['target_recall']:.2f} "
            + ("met." if chosen["target_met"] else "NOT MET: fell back to the best-F1 threshold.")
        )
    gate_set = report.get("gate_set")
    if gate_set:
        for lang, r in sorted(gate_set["by_language"].items()):
            lines.append(
                f"Gate set {lang}: {r['n']} rows, P0 recall {fmt_rate(r['p0_recall_ci'])}, "
                f"macro-F1 {r['macro_f1']:.3f}."
            )
    lines += [
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
    ap.add_argument(
        "--no-promote", action="store_true", help="register a candidate; do not run the gate"
    )
    args = ap.parse_args()
    model, report = train(
        args.data,
        args.out,
        class_weight=None if args.no_class_weight else "balanced",
        calibrate=not args.no_calibration,
        target_recall=args.target_recall,
        min_precision=args.min_precision,
        promote=not args.no_promote,
    )
    print(f"model {model.version}\n")
    print(format_report(report))
    if "promotion" in report:
        from nw.triage.promote import Decision, format_decision

        print()
        print(format_decision(Decision(**report["promotion"])))
        return 0 if report["promotion"]["passed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
