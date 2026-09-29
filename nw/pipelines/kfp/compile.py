"""Compile both pipelines to YAML.

    python -m nw.pipelines.kfp.compile --image <registry>/nw-pipelines:<tag> --out <dir>

The image is baked into the YAML, so compile once per image tag. Each pipeline is written
twice, `triage.yaml` and `retrain-triage.yaml` (the name the weekly jobs use). The Google Cloud
platform submits the YAML to Vertex AI Pipelines; `run_local` feeds it to the Kubeflow local
runner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nw.pipelines import COMPILED_DIR, DEFAULT_IMAGE
from nw.pipelines.kfp.pipelines import FACTORIES, compile_one


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", default=DEFAULT_IMAGE, help="the nw-pipelines image tag")
    ap.add_argument("--out", type=Path, default=COMPILED_DIR)
    ap.add_argument("--pipeline", choices=sorted(FACTORIES), default=None, help="default: both")
    args = ap.parse_args(argv)
    names = [args.pipeline] if args.pipeline else sorted(FACTORIES)
    for name in names:
        print(compile_one(name, args.out, args.image))
    return 0


if __name__ == "__main__":
    sys.exit(main())
