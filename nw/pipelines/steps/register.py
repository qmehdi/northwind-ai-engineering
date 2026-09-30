"""The last step of both pipelines: register the candidate that cleared the gate.

    python -m nw.pipelines.steps.register --pipeline triage --out artifacts/triage
    python -m nw.pipelines.steps.register --pipeline semantic --out artifacts/semantic

The only step that talks to the platform, and only to its `ModelRegistry`: the track's
implementation from `platform_for(settings)` under the tenant from `NW_TENANT` and
`NW_ENVIRONMENT`. A candidate whose gate did not pass is refused with the gate's reasons,
which fails the pipeline the way the retraining workflows fail their job. The registered
version starts as a candidate; approval is a human action in the registry (ADR 0008).

Every track registers through this step, so a version carries the same tags everywhere:
`source=pipeline` (a laptop run never registers here; `bootstrap` writes `source=bootstrap`),
the pipeline, the artifact version, the gate and its champion, the trigger, and the lineage
(`nw.platform.lineage`: the commit, the `uv.lock` hash, the image digest and the source bundle's
sha, which `nw.pipelines.source` exports inside a cloud step).

`NW_PIPELINE_REGISTRY=module:factory` swaps the registry for the one the factory returns.
That is the seam the tests use to run the pipeline end to end without a platform.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nw.pipelines.steps import local_path, read_json, read_result, write_json, write_result
from nw.platform.base import ModelRegistry, ModelVersion, Tenant
from nw.platform.lineage import lineage
from nw.platform.lineage import resolve as resolve_git_sha

STEP = "register"
GATE_STEP = {"triage": "triage_evaluate", "semantic": "semantic_gate"}
REGISTRY_ENV = "NW_PIPELINE_REGISTRY"


def registry_from_env() -> ModelRegistry:
    factory = os.environ.get(REGISTRY_ENV)
    if factory:
        module, _, name = factory.partition(":")
        return getattr(importlib.import_module(module), name or "build")()
    from nw.config import settings
    from nw.platform import platform_for

    return platform_for(settings()).registry


def tenant_from_args(tenant: str | None, environment: str | None) -> Tenant:
    from nw.config import settings
    from nw.platform.base import tenant_from_env

    default = tenant_from_env(settings())
    return Tenant(name=tenant or default.name, environment=environment or default.environment)


def registration_tags(
    pipeline: str, version: str, metadata: dict[str, Any], decision: dict[str, Any], trigger: str
) -> dict[str, str]:
    """The tags every registered version carries, on every track."""
    found = lineage()
    recorded = str(metadata.get("git_sha", ""))
    if recorded and recorded != "nogit":
        found["git_sha_source"] = "artifact"
    tags = {
        "source": "pipeline",
        "pipeline": pipeline,
        "artifact_version": version,
        "data_sha256_12": str(metadata.get("data_sha256_12", "")),
        "gate": "forced" if decision.get("forced") else "passed",
        "production": str(decision.get("production") or "none"),
        "champion": str(decision.get("champion_source") or "summary"),
        "served_format": str(decision.get("served_format") or ""),
        "trigger": trigger or "manual",
        **found,
        "git_sha": resolve_git_sha(recorded),
    }
    return {k: v for k, v in tags.items() if v != ""}


def run(
    out: Path | str,
    version: str | None,
    *,
    pipeline: str,
    registry: ModelRegistry,
    tenant: Tenant,
    name: str | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    out = local_path(out)
    if pipeline not in GATE_STEP:
        raise SystemExit(f"unknown pipeline {pipeline!r}; expected one of {sorted(GATE_STEP)}")
    decision = read_result(out, GATE_STEP[pipeline])
    version = version or decision["candidate"]
    if decision["candidate"] != version:
        raise SystemExit(
            f"the gate decided on {decision['candidate']}, not {version}: rerun the gate"
        )
    if not decision["passed"]:
        raise SystemExit(f"gate failed for {version}, not registering: {decision['reason']}")
    metadata = read_json(out / version / "metadata.json")
    metrics = {k: float(v) for k, v in decision["metrics"].items() if isinstance(v, int | float)}
    tags = registration_tags(pipeline, version, metadata, decision, trigger)
    registered: ModelVersion = registry.register(
        tenant, name or pipeline, out / version, metrics, tags
    )
    result = {
        "step": STEP,
        "pipeline": pipeline,
        "version": version,
        "tenant": tenant.prefix,
        "registered": asdict(registered) if hasattr(registered, "__dataclass_fields__") else None,
    }
    write_json(out / version / "register.json", result)
    write_result(out, STEP, result)
    print(
        f"registered {tenant.resource(name or pipeline)} version {registered.version} "
        f"({registered.stage}) from {version}"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", choices=sorted(GATE_STEP), required=True)
    ap.add_argument("--out", required=True, help="the run's tree: a path or gs:// URI")
    ap.add_argument("--version", default=None, help="default: the version the gate decided on")
    ap.add_argument("--name", default=None, help="registry name; default is the pipeline name")
    ap.add_argument("--tenant", default=None, help="default NW_TENANT, else solo")
    ap.add_argument("--environment", default=None, help="default NW_ENVIRONMENT, else northwind")
    ap.add_argument("--trigger", default="manual", help="manual or schedule, for the record")
    args = ap.parse_args(argv)
    run(
        args.out,
        args.version,
        pipeline=args.pipeline,
        registry=registry_from_env(),
        tenant=tenant_from_args(args.tenant, args.environment),
        name=args.name,
        trigger=args.trigger,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
