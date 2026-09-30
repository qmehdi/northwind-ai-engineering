"""Project 2, step 4: the benchmark against Project 1, into the candidate.

    python -m nw.pipelines.steps.semantic_benchmark --out artifacts/semantic \
        --data data/tickets.jsonl --triage artifacts/triage/latest

`nw.semantic.benchmark.run` needs a Project 1 artifact on the same test split. A pipeline
run has none at hand, so without `--triage` the step trains a Project 1 candidate on the
same data under `<version>/benchmark_triage/`, as the retraining job does, and compares
against that. The table lands in `<version>/benchmark.json`, where the gate reads it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from nw.pipelines.steps import local_path, localize, write_result
from nw.semantic.artifacts import newest_candidate
from nw.semantic.benchmark import format_table
from nw.semantic.benchmark import run as benchmark
from nw.semantic.benchmark import write as write_benchmark

STEP = "semantic_benchmark"


def reference_triage(data: Path, into: Path) -> Path:
    """A Project 1 candidate trained on the same data, registered and not promoted."""
    from nw.triage.train import train as train_triage

    model, _ = train_triage(data, into, promote=False)
    return into / model.version


def run(
    out: Path,
    version: str | None,
    data: Path,
    *,
    triage: Path | None = None,
    n: int | None = None,
    latency_n: int = 100,
    config: Any = None,
    tokenizer: Any = None,
) -> dict[str, Any]:
    out = local_path(out)
    version = version or newest_candidate(out).name
    data = localize(data)
    triage = local_path(triage) if triage else None
    if triage is None or not (triage / "metadata.json").exists():
        triage = reference_triage(data, out / version / "benchmark_triage")
    results = benchmark(
        Path(triage),
        out / version,
        data,
        n,
        latency_n,
        config=config,
        tokenizer=tokenizer,
    )
    write_benchmark(out / version, results)
    int8 = next(r for r in results if r["model"].endswith("ONNX int8"))
    result = {
        "step": STEP,
        "version": version,
        "artifact": str(out / version),
        "triage": str(triage),
        "rows": [r["model"] for r in results],
        "metrics": {
            "int8_priority_macro_f1": int8["priority_macro_f1"],
            "int8_p0_recall": int8["p0_recall"],
            "int8_tag_micro_f1": int8["tag_micro_f1"],
            "int8_p50_ms": int8["p50_ms"],
            "int8_p95_ms": int8["p95_ms"],
        },
    }
    write_result(out, STEP, result)
    print(format_table(results))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out", default="artifacts/semantic", help="the artifact tree: a path or gs:// URI"
    )
    ap.add_argument("--version", default=None, help="artifact version; default is the newest")
    ap.add_argument("--data", default="data/tickets.jsonl", help="a path, gs:// or s3:// URI")
    ap.add_argument(
        "--triage", default=None, help="a Project 1 artifact (path or URI); default trains one"
    )
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--latency-n", type=int, default=100)
    args = ap.parse_args(argv)
    triage = args.triage if args.triage and str(args.triage) else None
    run(args.out, args.version, args.data, triage=triage, n=args.n, latency_n=args.latency_n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
