"""Backtest two model versions on the same tickets before the new one takes traffic.

    uv run python -m nw.triage.backtest --a artifacts/triage/latest --b artifacts/triage/<candidate>
    uv run python -m nw.triage.backtest --a ... --b ... --data artifacts/triage/predictions.jsonl

With labelled rows (the test split) it reports each model's metrics side by side and
the agreement between them; with captured production requests (no labels) it reports
agreement and where the two disagree. Shadow mode in the service is the live version
of the same comparison.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from nw.triage.model import TriageModel
from nw.triage.train import evaluate, load_tickets


def compare(a: TriageModel, b: TriageModel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pa = [r.priority for r in a.predict(rows)]
    pb = [r.priority for r in b.predict(rows)]
    agree = sum(1 for x, y in zip(pa, pb, strict=True) if x == y)
    moves = Counter(f"{x}->{y}" for x, y in zip(pa, pb, strict=True) if x != y)
    out: dict[str, Any] = {
        "n": len(rows),
        "agreement": agree / max(len(rows), 1),
        "disagreements": dict(moves.most_common(8)),
        "a": {"version": a.version, "predicted_share": _share(pa)},
        "b": {"version": b.version, "predicted_share": _share(pb)},
    }
    if rows and all("priority" in r for r in rows):
        out["a"]["metrics"] = evaluate(a, rows)
        out["b"]["metrics"] = evaluate(b, rows)
    return out


def _share(preds: list[str]) -> dict[str, float]:
    c = Counter(preds)
    return {k: c.get(k, 0) / max(len(preds), 1) for k in ("P0", "P1", "P2", "P3")}


def format_backtest(r: dict[str, Any]) -> str:
    lines = [f"{r['n']} tickets, agreement {r['agreement']:.3f}"]
    for side in ("a", "b"):
        s = r[side]
        line = f"  {side}: {s['version']}  predicted share " + " ".join(
            f"{k} {v:.2f}" for k, v in s["predicted_share"].items()
        )
        if "metrics" in s:
            m = s["metrics"]
            line += (
                f"  macro-F1 {m['macro_f1']:.3f} P0 recall {m['p0_recall']:.3f} ECE {m['ece']:.3f}"
            )
        lines.append(line)
    if r["disagreements"]:
        lines.append(
            "  disagreements " + ", ".join(f"{k} {v}" for k, v in r["disagreements"].items())
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True)
    ap.add_argument("--b", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument(
        "--split", default="test", help="split to use when the data has one; empty for all rows"
    )
    args = ap.parse_args()
    rows = load_tickets(args.data)
    if rows and "model_version" in rows[0]:
        # a capture file: the served prediction is not a label, so keep it out of the metrics
        rows = [{k: v for k, v in r.items() if k != "priority"} for r in rows]
    elif args.split:
        rows = [r for r in rows if r.get("split", args.split) == args.split]
    result = compare(TriageModel.load(args.a), TriageModel.load(args.b), rows)
    print(format_backtest(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
