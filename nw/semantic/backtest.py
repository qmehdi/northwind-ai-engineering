"""Backtest two Project 2 versions on the same tickets before the new one takes traffic.

    uv run python -m nw.semantic.backtest --a artifacts/semantic/latest \
        --b artifacts/semantic/<candidate>
    uv run python -m nw.semantic.backtest --a ... --b ... \
        --data artifacts/semantic/predictions.jsonl --split ""

Both versions run as the service runs them: the exported graph on ONNX Runtime, the one the
promotion gate chose for that version (`serving.json`: int8 only when it cleared every bar);
`--fp32` or `--int8` forces one graph for both. With labelled rows (the test split) it
reports each version's metrics side by side and the agreement between them on priority and
on the tag set; with captured
requests (`NW_SEMANTIC_CAPTURE`, no labels) it reports agreement and where they part.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.metrics import f1_score

from nw.semantic.artifacts import resolve
from nw.semantic.benchmark import priority_scores
from nw.semantic.data import load_rows, multi_hot
from nw.triage.features import PRIORITIES, ticket_text


class Predictor:
    """One artifact version, loaded the way the service loads it."""

    def __init__(self, artifact: Path | str, *, quantized: bool | None = None) -> None:
        from transformers import AutoTokenizer

        from nw.semantic.export import OnnxEncoder

        self.dir = resolve(artifact)
        meta = json.loads((self.dir / "metadata.json").read_text(encoding="utf-8"))
        tok_dir = self.dir / "tokenizer"
        tokenizer = AutoTokenizer.from_pretrained(
            str(tok_dir) if tok_dir.exists() else meta["base"]
        )
        if quantized is None:  # as served: the gate's choice, int8 for a version never gated
            from nw.semantic.promote import served_quantized

            chosen = served_quantized(self.dir)
            quantized = True if chosen is None else chosen
        file = (
            "model.int8.onnx"
            if quantized and (self.dir / "model.int8.onnx").exists()
            else "model.onnx"
        )
        if not (self.dir / file).exists():
            raise SystemExit(f"{self.dir} has no {file}: run the export first")
        self.onnx = OnnxEncoder(self.dir / file, tokenizer, meta["max_length"])
        self.thresholds = np.load(self.dir / "tag_thresholds.npy")
        self.version, self.format = meta["version"], file

    def predict(self, texts: list[str], batch: int = 32) -> tuple[np.ndarray, np.ndarray]:
        """Tag decisions (bool matrix) and priority index per text."""
        tags, prios = [], []
        for i in range(0, len(texts), batch):
            tl, pl, _ = self.onnx.run(texts[i : i + batch])
            tags.append((1 / (1 + np.exp(-tl))) >= self.thresholds)
            prios.append(pl.argmax(1))
        return np.concatenate(tags), np.concatenate(prios)


def _jaccard(a: np.ndarray, b: np.ndarray) -> float:
    inter = (a & b).sum(1)
    union = (a | b).sum(1)
    return float(np.mean(np.where(union > 0, inter / np.maximum(union, 1), 1.0)))


def compare(a: Predictor, b: Predictor, rows: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [ticket_text(r.get("subject", ""), r.get("body", "")) for r in rows]
    ta, pa = a.predict(texts)
    tb, pb = b.predict(texts)
    moves = Counter(
        f"{PRIORITIES[x]}->{PRIORITIES[y]}" for x, y in zip(pa, pb, strict=True) if x != y
    )
    out: dict[str, Any] = {
        "n": len(rows),
        "priority_agreement": float((pa == pb).mean()) if len(rows) else 1.0,
        "tag_set_agreement": float((ta == tb).all(1).mean()) if len(rows) else 1.0,
        "tag_jaccard": _jaccard(ta, tb) if len(rows) else 1.0,
        "disagreements": dict(moves.most_common(8)),
        "a": {"version": a.version, "format": a.format, "predicted_share": _share(pa)},
        "b": {"version": b.version, "format": b.format, "predicted_share": _share(pb)},
    }
    if rows and all("priority" in r and "tags" in r for r in rows):
        y_prio = np.asarray([PRIORITIES.index(r["priority"]) for r in rows])
        y_tags = np.stack([multi_hot(r["tags"]).numpy() for r in rows]).astype(bool)
        for side, tags, prio in (("a", ta, pa), ("b", tb, pb)):
            out[side]["metrics"] = {
                **priority_scores(prio, y_prio),
                "tag_micro_f1": float(f1_score(y_tags, tags, average="micro", zero_division=0)),
                "tag_macro_f1": float(f1_score(y_tags, tags, average="macro", zero_division=0)),
            }
    return out


def _share(pred: np.ndarray) -> dict[str, float]:
    c = Counter(int(x) for x in pred)
    return {p: c.get(i, 0) / max(len(pred), 1) for i, p in enumerate(PRIORITIES)}


def format_backtest(r: dict[str, Any]) -> str:
    lines = [
        f"{r['n']} tickets, priority agreement {r['priority_agreement']:.3f}, tag set agreement "
        f"{r['tag_set_agreement']:.3f}, tag Jaccard {r['tag_jaccard']:.3f}"
    ]
    for side in ("a", "b"):
        s = r[side]
        line = f"  {side}: {s['version']} ({s['format']})  predicted share " + " ".join(
            f"{k} {v:.2f}" for k, v in s["predicted_share"].items()
        )
        if "metrics" in s:
            m = s["metrics"]
            line += (
                f"  tag micro-F1 {m['tag_micro_f1']:.3f} priority macro-F1 "
                f"{m['priority_macro_f1']:.3f} P0 recall {m['p0_recall']:.3f}"
            )
        lines.append(line)
    if r["disagreements"]:
        lines.append(
            "  disagreements " + ", ".join(f"{k} {v}" for k, v in r["disagreements"].items())
        )
    return "\n".join(lines)


def as_served(a: Path, b: Path) -> tuple[bool, bool]:
    """Each version with the graph its gate chose. A version never gated (a fresh candidate)
    takes the other's choice, as the service's shadow does, so a backtest and the shadow compare
    the same graphs; with neither gated, int8."""
    from nw.semantic.promote import served_quantized

    ca, cb = served_quantized(Path(a)), served_quantized(Path(b))
    if ca is None:
        ca = cb
    if cb is None:
        cb = ca
    return (True if ca is None else ca), (True if cb is None else cb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True)
    ap.add_argument("--b", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument(
        "--split", default="test", help="split to use when the data has one; empty for all rows"
    )
    graph = ap.add_mutually_exclusive_group()
    graph.add_argument("--fp32", action="store_true", help="force the fp32 graphs")
    graph.add_argument("--int8", action="store_true", help="force the int8 graphs")
    args = ap.parse_args()
    rows = load_rows(args.data)
    if args.split:
        rows = [r for r in rows if r.get("split", args.split) == args.split]
    if args.fp32 or args.int8:
        qa = qb = not args.fp32
    else:
        qa, qb = as_served(args.a, args.b)
    result = compare(Predictor(args.a, quantized=qa), Predictor(args.b, quantized=qb), rows)
    print(format_backtest(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
