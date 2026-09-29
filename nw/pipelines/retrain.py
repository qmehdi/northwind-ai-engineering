"""Submit a retraining run through the active track's platform.

    uv run python -m nw.pipelines.retrain --pipeline triage
    uv run python -m nw.pipelines.retrain --pipeline semantic --epochs 2 --wait
    uv run python -m nw.pipelines.retrain --pipeline triage --min-p0-recall 0.9 --data-uri s3://...

The weekly jobs (EventBridge Scheduler on AWS, Cloud Scheduler on Google Cloud, a compose
cron on the Local track) start the same pipelines with the same parameters; this is the hand
version, for a learner at a laptop or the `pipeline-submit` Makefile target. Every parameter
in `nw.pipelines.params` is a flag; the platform's `PipelineRunner` maps the names to its SDK
(`sagemaker_parameter_name` for SageMaker; Vertex and the local runner take them as they are).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from nw.pipelines import NAMES, canonical
from nw.pipelines.params import BY_PIPELINE, Param
from nw.platform.base import Platform, RunStatus, Tenant


def add_parameter_flags(ap: argparse.ArgumentParser, params: tuple[Param, ...]) -> None:
    for p in params:
        flag = f"--{p.name.replace('_', '-')}"
        if p.kind is bool:
            ap.add_argument(
                flag,
                default=None,
                choices=["true", "false"],
                help=f"{p.help} (default {str(p.default).lower()})",
            )
        else:
            ap.add_argument(flag, type=p.kind, default=None, help=f"{p.help} (default {p.default})")


def parameters_from(args: argparse.Namespace, params: tuple[Param, ...]) -> dict[str, Any]:
    """Only what was set on the command line; the pipeline keeps its defaults for the rest."""
    out: dict[str, Any] = {}
    for p in params:
        value = getattr(args, p.name)
        if value is None:
            continue
        out[p.name] = (value == "true") if p.kind is bool else value
    return out


def submit(
    platform: Platform,
    tenant: Tenant,
    pipeline: str,
    params: dict[str, Any],
    *,
    wait: bool = False,
    timeout_s: float = 3600,
) -> Any:
    run = platform.pipelines.submit(tenant, pipeline, params)
    print(f"submitted {pipeline} for {tenant.prefix}: run {run.run_id} {run.status}")
    if run.url:
        print(run.url)
    if not wait and getattr(platform.pipelines, "runs_in_process", False):
        # The Local track's runner executes in this process: returning would end the run.
        print("the local runner runs in this process: following the run to its end")
        wait = True
    if wait:
        run = platform.pipelines.wait(tenant, run, timeout_s=timeout_s)
        print(f"run {run.run_id} {run.status}")
        for key, value in run.outputs.items():
            print(f"  {key}: {value}")
    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", choices=sorted(NAMES), required=True)
    ap.add_argument("--wait", action="store_true", help="follow the run to its end")
    ap.add_argument("--timeout", type=float, default=3600, help="seconds to wait for the run")
    first = ap.parse_known_args(argv)[0]
    params = BY_PIPELINE[canonical(first.pipeline)]
    add_parameter_flags(ap, params)
    args = ap.parse_args(argv)

    from nw.config import settings
    from nw.platform import platform_for
    from nw.platform.base import tenant_from_env

    cfg = settings()
    tenant = tenant_from_env(cfg)
    values = parameters_from(args, params)
    values.setdefault("tenant", tenant.name)
    values.setdefault("environment", tenant.environment)
    run = submit(
        platform_for(cfg),
        tenant,
        args.pipeline,
        values,
        wait=args.wait,
        timeout_s=args.timeout,
    )
    return 0 if run.status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.SUCCEEDED) else 1


if __name__ == "__main__":
    sys.exit(main())
