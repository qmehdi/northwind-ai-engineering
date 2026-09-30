"""Project 2, step 3: ONNX export, quantisation and parity, into the candidate.

    python -m nw.pipelines.steps.semantic_export --out artifacts/semantic --data data/tickets.jsonl

`nw.semantic.export.run` on one version directory. With `--package-dir` the candidate, now
carrying the int8 graph and the tokenizer, is also written as `model.tar.gz` for the register
step on SageMaker. Exit 1 when the fp32 graph does not match PyTorch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from nw.pipelines.steps import local_path, localize, package, write_result
from nw.semantic.artifacts import newest_candidate
from nw.semantic.export import run as export

STEP = "semantic_export"


def run(
    out: Path,
    version: str | None,
    data: Path,
    *,
    n: int = 20,
    package_dir: Path | None = None,
    config: Any = None,
    tokenizer: Any = None,
) -> dict[str, Any]:
    out = local_path(out)
    version = version or newest_candidate(out).name
    report = export(out / version, localize(data), n, config=config, tokenizer=tokenizer)
    result = {
        "step": STEP,
        "version": version,
        "artifact": str(out / version),
        **report,
        "packaged": str(package(out / version, package_dir)) if package_dir else None,
    }
    write_result(out, STEP, result)
    print(
        f"export {version}: fp32 parity {report['max_abs_diff_fp32']:.2e}, "
        f"int8 {report['max_abs_diff_int8']:.2e}, size ratio {report['size_ratio']}"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out", default="artifacts/semantic", help="the artifact tree: a path or gs:// URI"
    )
    ap.add_argument("--version", default=None, help="artifact version; default is the newest")
    ap.add_argument("--data", default="data/tickets.jsonl", help="a path, gs:// or s3:// URI")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--package-dir", default=None)
    args = ap.parse_args(argv)
    result = run(args.out, args.version, args.data, n=args.n, package_dir=args.package_dir)
    return 0 if result["parity_fp32_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
