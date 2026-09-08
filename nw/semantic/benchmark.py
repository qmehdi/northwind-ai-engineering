"""The benchmark harness: Project 1 against Project 2 in its three forms.

    uv run python -m nw.semantic.benchmark --triage artifacts/triage/latest \
        --semantic artifacts/semantic

One table, same test split, same machine: priority macro-F1 and P0 recall,
tag micro-F1 where the model has tags, p50 and p95 latency per ticket at batch
size one on CPU, artifact size, and cost per thousand tickets at a stated
CPU price. The table is the deliverable; nobody remembers the loss curve.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import f1_score, recall_score

from nw.semantic.data import load_rows, multi_hot
from nw.semantic.export import OnnxEncoder, load_finetuned
from nw.triage.features import PRIORITIES, ticket_text
from nw.triage.model import TriageModel

CPU_USD_PER_HOUR = 0.04  # one vCPU on a small cloud instance; replace from the cost sheet


def latency(fn, items: list[Any], warmup: int = 5) -> tuple[float, float]:
    for x in items[:warmup]:
        fn(x)
    times = []
    for x in items:
        t0 = time.perf_counter()
        fn(x)
        times.append((time.perf_counter() - t0) * 1000)
    return float(np.percentile(times, 50)), float(np.percentile(times, 95))


def priority_scores(pred: np.ndarray, y: np.ndarray) -> dict[str, float]:
    return {
        "priority_macro_f1": float(
            f1_score(y, pred, average="macro", labels=list(range(len(PRIORITIES))), zero_division=0)
        ),
        "p0_recall": float(recall_score(y == 0, pred == 0, zero_division=0)),
    }


def dir_size(path: Path) -> int:
    return (
        sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        if path.is_dir()
        else path.stat().st_size
    )


def run(
    triage_dir: Path, semantic_dir: Path, data: Path, n: int | None = None, latency_n: int = 100
) -> list[dict[str, Any]]:
    rows = load_rows(data, "test")
    if n:
        rows = rows[:n]
    texts = [ticket_text(r["subject"], r["body"]) for r in rows]
    y_prio = np.asarray([PRIORITIES.index(r["priority"]) for r in rows])
    y_tags = np.stack([multi_hot(r["tags"]).numpy() for r in rows])
    results: list[dict[str, Any]] = []

    # Project 1
    triage = TriageModel.load(triage_dir)
    pred = np.asarray([PRIORITIES.index(triage.decide(p)[0]) for p in triage.predict_proba(rows)])
    p50, p95 = latency(lambda r: triage.predict([r]), rows[:latency_n])
    results.append(
        {
            "model": "Project 1: TF-IDF + logistic regression",
            **priority_scores(pred, y_prio),
            "tag_micro_f1": None,
            "p50_ms": p50,
            "p95_ms": p95,
            "bytes": dir_size(triage_dir),
        }
    )

    # Project 2, PyTorch fp32
    model, tokenizer, meta = load_finetuned(semantic_dir)
    thresholds = np.load(semantic_dir / "tag_thresholds.npy")
    torch.set_num_threads(max(1, torch.get_num_threads()))

    def torch_predict(batch_texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        with torch.no_grad():
            enc = tokenizer(
                batch_texts,
                padding=True,
                truncation=True,
                max_length=meta["max_length"],
                return_tensors="pt",
            )
            tl, pl = model(enc["input_ids"], enc["attention_mask"])
        return torch.sigmoid(tl).numpy(), pl.numpy()

    tag_p, prio_l = [], []
    for i in range(0, len(texts), 32):
        a, b = torch_predict(texts[i : i + 32])
        tag_p.append(a)
        prio_l.append(b)
    tag_p, prio_l = np.concatenate(tag_p), np.concatenate(prio_l)
    p50, p95 = latency(lambda t: torch_predict([t]), texts[:latency_n])
    results.append(
        {
            "model": "Project 2: PyTorch fp32",
            **priority_scores(prio_l.argmax(1), y_prio),
            "tag_micro_f1": float(
                f1_score(y_tags, tag_p >= thresholds, average="micro", zero_division=0)
            ),
            "p50_ms": p50,
            "p95_ms": p95,
            "bytes": (semantic_dir / "best.pt").stat().st_size
            + dir_size(Path(torch.hub.get_dir())) * 0,
        }
    )

    for name, file in (
        ("Project 2: ONNX fp32", "model.onnx"),
        ("Project 2: ONNX int8", "model.int8.onnx"),
    ):
        path = semantic_dir / file
        if not path.exists():
            continue
        onnx = OnnxEncoder(path, tokenizer, meta["max_length"])
        tag_p, prio_l = [], []
        for i in range(0, len(texts), 32):
            a, b, _ = onnx.run(texts[i : i + 32])
            tag_p.append(1 / (1 + np.exp(-a)))
            prio_l.append(b)
        tag_p, prio_l = np.concatenate(tag_p), np.concatenate(prio_l)
        p50, p95 = latency(lambda t, o=onnx: o.run([t]), texts[:latency_n])
        results.append(
            {
                "model": name,
                **priority_scores(prio_l.argmax(1), y_prio),
                "tag_micro_f1": float(
                    f1_score(y_tags, tag_p >= thresholds, average="micro", zero_division=0)
                ),
                "p50_ms": p50,
                "p95_ms": p95,
                "bytes": path.stat().st_size,
            }
        )

    for r in results:
        r["usd_per_1k_tickets"] = round(r["p50_ms"] / 1000 / 3600 * CPU_USD_PER_HOUR * 1000, 5)
    return results


def format_table(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Model | Priority macro-F1 | P0 recall | Tag micro-F1 | p50 ms | p95 ms "
        "| Size MB | USD per 1k |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        tag = "n/a" if r["tag_micro_f1"] is None else f"{r['tag_micro_f1']:.3f}"
        lines.append(
            f"| {r['model']} | {r['priority_macro_f1']:.3f} | {r['p0_recall']:.3f} | {tag} "
            f"| {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['bytes'] / 1e6:.1f} "
            f"| {r['usd_per_1k_tickets']:.4f} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--triage", type=Path, default=Path("artifacts/triage/latest"))
    ap.add_argument("--semantic", type=Path, default=Path("artifacts/semantic"))
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--n", type=int, default=None)
    args = ap.parse_args()
    results = run(args.triage, args.semantic, args.data, args.n)
    (args.semantic / "benchmark.json").write_text(json.dumps(results, indent=1))
    print(format_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
