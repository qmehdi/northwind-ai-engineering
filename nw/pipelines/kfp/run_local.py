"""Run a pipeline on the Kubeflow local runner.

    python -m nw.pipelines.kfp.run_local --pipeline triage                    # this interpreter
    python -m nw.pipelines.kfp.run_local --pipeline triage --runner docker --image <tag>
    python -m nw.pipelines.kfp.run_local --pipeline semantic --set epochs=1 --set subset=500

`subprocess` runs the steps in the current virtualenv, which is the Local track on a laptop
with no image built yet. `docker` runs them in the course image, the same way Vertex does,
with the checkout mounted so data and outputs stay on disk. Every pipeline parameter can be
set with `--set name=value`; the rest keep the defaults from `nw.pipelines.params`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kfp import local

from nw.pipelines import DEFAULT_IMAGE
from nw.pipelines.kfp.pipelines import FACTORIES
from nw.pipelines.params import BY_PIPELINE

PASS_THROUGH_ENV = (
    "NW_TRACK",
    "NW_TENANT",
    "NW_ENVIRONMENT",
    "NW_MLFLOW_URI",
    "NW_PIPELINE_REGISTRY",
)


@contextlib.contextmanager
def kfp_interpreter() -> Iterator[None]:
    """The SubprocessRunner splices `sys.executable` unquoted into an `sh -c` string, so a
    checkout under a path with a space fails before the step starts. Quoting the path for
    the shell while the runner reads it is the whole fix; the step processes see the real
    path because they start from the executor, not from the string."""
    real = sys.executable
    if " " not in real:
        yield
        return
    sys.executable = shlex.quote(real)
    try:
        yield
    finally:
        sys.executable = real


def coerce(pipeline: str, overrides: dict[str, str]) -> dict[str, Any]:
    kinds = {p.name: p.kind for p in BY_PIPELINE[pipeline]}
    out: dict[str, Any] = {}
    for name, raw in overrides.items():
        if name not in kinds:
            raise SystemExit(f"{pipeline} has no parameter {name!r}; see nw/pipelines/params.py")
        kind = kinds[name]
        if kind is bool:
            out[name] = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            out[name] = kind(raw)
    return out


def run(
    pipeline: str,
    params: dict[str, Any],
    *,
    runner: str = "subprocess",
    image: str = DEFAULT_IMAGE,
    pipeline_root: Path = Path("artifacts/pipelines/local"),
) -> Any:
    if runner == "docker":
        cwd = str(Path.cwd())
        env = {k: os.environ[k] for k in PASS_THROUGH_ENV if k in os.environ}
        local.init(
            runner=local.DockerRunner(volumes={cwd: {"bind": cwd, "mode": "rw"}}, environment=env),
            pipeline_root=str(pipeline_root),
        )
    else:
        local.init(runner=local.SubprocessRunner(use_venv=False), pipeline_root=str(pipeline_root))
    with kfp_interpreter():
        return FACTORIES[pipeline](image)(**params)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", choices=sorted(FACTORIES), required=True)
    ap.add_argument("--runner", choices=["subprocess", "docker"], default="subprocess")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--pipeline-root", type=Path, default=Path("artifacts/pipelines/local"))
    ap.add_argument(
        "--set", action="append", default=[], metavar="NAME=VALUE", help="a pipeline parameter"
    )
    args = ap.parse_args(argv)
    overrides = dict(item.split("=", 1) for item in args.set)
    result = run(
        args.pipeline,
        coerce(args.pipeline, overrides),
        runner=args.runner,
        image=args.image,
        pipeline_root=args.pipeline_root,
    )
    print(result.outputs.get("Output") if hasattr(result, "outputs") else result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
