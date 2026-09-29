"""The Model Monitor baseline: `statistics.json` and `constraints.json` from the training profile.

    uv run python -m nw.serving.sagemaker.baseline \
        --profile artifacts/triage/latest/data_profile.json \
        --out artifacts/triage/latest/baseline [--tickets data/tickets.jsonl]

Model Monitor compares captured traffic with a baseline of per-feature statistics and
constraints (the data quality monitor's file formats: `statistics.json` with `dataset.item_count`
and one `numerical_statistics` block per feature, `constraints.json` with completeness, numeric
constraints and `monitoring_config.distribution_constraints`). The one feature here is
`text_length`, subject plus body in characters: the signal the course services already track for
drift, so the managed monitor and `/drift` alarm on the same thing (`deploy/aws/stacks/areas/
serving.py`, `MONITOR_FEATURE`).

The histogram buckets come straight from the profile's `text_length_bins` and
`text_length_hist` (the drift monitor's own bins). Mean, standard deviation, min and max are exact
when the tickets file is given and approximated from bucket midpoints otherwise. The comparison
threshold is 0.1, the same bar as the service's `watch` level.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

FEATURE = "text_length"
COMPARISON_THRESHOLD = 0.1


def _lengths(tickets: Path) -> list[int]:
    out = []
    for line in tickets.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("split", "train") == "train":
                out.append(len(row.get("subject", "")) + len(row.get("body", "")))
    return out


def buckets(profile: dict[str, Any], n: int) -> list[dict[str, float]]:
    edges = [float(x) for x in profile["text_length_bins"]]
    shares = [float(x) for x in profile["text_length_hist"]]
    if len(edges) != len(shares) + 1:
        raise ValueError("text_length_bins must have one more edge than text_length_hist")
    top = float(profile.get("text_length_quantiles", {}).get("0.99", 0.0)) or edges[-2] * 2
    out = []
    for lower, upper, share in zip(edges[:-1], edges[1:], shares, strict=True):
        hi = top if math.isinf(upper) else upper
        out.append({"lower_bound": lower, "upper_bound": hi, "count": round(share * n)})
    return out


def statistics(profile: dict[str, Any], lengths: list[int] | None = None) -> dict[str, Any]:
    n = int(profile.get("splits", {}).get("train") or profile.get("n") or 0)
    if lengths:
        n = len(lengths)
        mean = sum(lengths) / n
        var = sum((x - mean) ** 2 for x in lengths) / n
        lo, hi, std = float(min(lengths)), float(max(lengths)), math.sqrt(var)
    else:
        bins = buckets(profile, n)
        total = sum(b["count"] for b in bins) or 1
        mids = [((b["lower_bound"] + b["upper_bound"]) / 2, b["count"]) for b in bins]
        mean = sum(m * c for m, c in mids) / total
        var = sum(((m - mean) ** 2) * c for m, c in mids) / total
        lo, hi, std = bins[0]["lower_bound"], bins[-1]["upper_bound"], math.sqrt(var)
    return {
        "version": 0,
        "dataset": {"item_count": n},
        "features": [
            {
                "name": FEATURE,
                "inferred_type": "Integral",
                "numerical_statistics": {
                    "common": {"num_present": n, "num_missing": 0},
                    "mean": round(mean, 3),
                    "sum": round(mean * n, 3),
                    "std_dev": round(std, 3),
                    "min": lo,
                    "max": hi,
                    "distribution": {"kll": {"buckets": buckets(profile, n)}},
                },
            }
        ],
    }


def constraints(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 0,
        "features": [
            {
                "name": FEATURE,
                "inferred_type": "Integral",
                "completeness": 1.0,
                "num_constraints": {"is_non_negative": True},
            }
        ],
        "monitoring_config": {
            "evaluate_constraints": "Enabled",
            "emit_metrics": "Enabled",
            "datatype_check_threshold": 1.0,
            "domain_content_threshold": 1.0,
            "distribution_constraints": {
                "perform_comparison": "Enabled",
                "comparison_threshold": COMPARISON_THRESHOLD,
                "comparison_method": "Robust",
            },
        },
        "data_sha256_12": profile.get("data_sha256_12", ""),
    }


def write_baseline(profile_path: Path, out: Path, tickets: Path | None = None) -> tuple[Path, Path]:
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    lengths = _lengths(Path(tickets)) if tickets else None
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stats_path = out / "statistics.json"
    cons_path = out / "constraints.json"
    stats_path.write_text(
        json.dumps(statistics(profile, lengths), indent=1) + "\n", encoding="utf-8"
    )
    cons_path.write_text(json.dumps(constraints(profile), indent=1) + "\n", encoding="utf-8")
    return stats_path, cons_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--profile", type=Path, default=Path("artifacts/triage/latest/data_profile.json")
    )
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage/latest/baseline"))
    ap.add_argument(
        "--tickets", type=Path, default=None, help="exact moments from the training split"
    )
    args = ap.parse_args(argv)
    for path in write_baseline(args.profile, args.out, args.tickets):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
