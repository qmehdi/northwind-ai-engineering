"""Mid-course recovery: put the promoted local artifacts into the platform registry, approved.

A learner who missed a part, or whose cloud pipeline never registered a version, has the code
(the solutions branch) but not the platform state: no registered version, no approval, nothing
for the capstone's promotion drill. `bootstrap` registers `artifacts/<name>/latest` (what the
laptop gate promoted) through the track's `ModelRegistry`, tagged `source=bootstrap` with the
same lineage tags a pipeline writes, and approves it with a reason that says where it came from.
A tenant that already has an approved or live version is left alone unless `--force`.

    uv run python -m nw.platform.aws bootstrap              # triage and semantic, AWS
    uv run python -m nw.platform.gcp bootstrap triage       # one project, Google Cloud
    uv run python -m nw.platform.azure bootstrap --force    # register again even if approved
    make bootstrap-aws | bootstrap-gcp | bootstrap-azure

The Local track's `python -m nw.platform.local bootstrap` does the same and also makes the
version live on the stack's serving tier.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nw.platform.base import ModelRegistry, ModelVersion, Stage, Tenant, newest

PROJECTS = ("triage", "semantic")
ROOT = Path("artifacts")
# The registry metrics a pipeline's register step writes, per project (the gate's test metrics).
METRIC_KEYS = {
    "triage": ("macro_f1", "p0_recall", "p0_precision", "ece", "brier_p0"),
    "semantic": ("tag_micro_f1", "tag_macro_f1", "priority_macro_f1", "p0_recall"),
}


def artifact_dir(name: str, root: Path = ROOT) -> Path:
    """`artifacts/<name>/latest`, resolved; it must hold a `metadata.json`."""
    path = Path(root) / name / "latest"
    if not (path / "metadata.json").is_file():
        raise FileNotFoundError(
            f"{path} has no promoted artifact: train and promote it on the laptop first "
            f"(`make train-{name}`, the gate sets `latest`), or restore it from the solutions "
            "branch"
        )
    return path.resolve()


def metrics_of(name: str, metadata: dict[str, Any]) -> dict[str, float]:
    test = (metadata.get("metrics") or {}).get("test") or {}
    out = {k: float(test[k]) for k in METRIC_KEYS.get(name, ()) if k in test}
    threshold = (metadata.get("metrics") or {}).get("p0_threshold")
    if threshold is not None:
        out["p0_threshold"] = float(threshold)
    return out


def tags_of(name: str, metadata: dict[str, Any]) -> dict[str, str]:
    from nw.platform.lineage import lineage
    from nw.platform.lineage import resolve as resolve_git_sha

    found = lineage()
    recorded = str(metadata.get("git_sha", ""))
    tags = {
        "source": "bootstrap",
        "pipeline": name,
        "artifact_version": str(metadata.get("version", "")),
        "data_sha256_12": str(metadata.get("data_sha256_12", "")),
        "gate": "laptop",
        "trigger": "bootstrap",
        **found,
        "git_sha": resolve_git_sha(recorded),
    }
    return {k: v for k, v in tags.items() if v}


def bootstrap(
    registry: ModelRegistry,
    tenant: Tenant,
    name: str,
    *,
    root: Path = ROOT,
    artifact: Path | None = None,
    approve: bool = True,
    force: bool = False,
    reason: str | None = None,
) -> tuple[ModelVersion, bool]:
    """The version the tenant can promote, and whether this call registered it."""
    held = newest(list(registry.versions(tenant, name)), Stage.LIVE, Stage.APPROVED)
    if held is not None and not force:
        return held, False
    path = Path(artifact).resolve() if artifact is not None else artifact_dir(name, root)
    if not (path / "metadata.json").is_file():
        raise FileNotFoundError(f"{path} holds no metadata.json: not a trained artifact")
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    version = registry.register(
        tenant, name, path, metrics_of(name, metadata), tags_of(name, metadata)
    )
    if approve:
        version = registry.set_stage(
            tenant,
            name,
            version.version,
            Stage.APPROVED,
            reason
            or f"bootstrap from {path.name}: promoted by the laptop gate; recovery, not a pipeline",
        )
    return version, True


def main(argv: Sequence[str] | None = None, *, track: str | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog=f"python -m nw.platform.{track or '<track>'} bootstrap", description=__doc__
    )
    ap.add_argument("names", nargs="*", default=list(PROJECTS), help="triage, semantic")
    ap.add_argument("--root", type=Path, default=ROOT, help="the artifact tree")
    ap.add_argument("--force", action="store_true", help="register even if a version is approved")
    ap.add_argument("--no-approve", action="store_true", help="register as a candidate only")
    args = ap.parse_args(list(argv or []))
    unknown = sorted(set(args.names) - set(PROJECTS))
    if unknown:
        ap.error(f"unknown project {', '.join(unknown)}; expected {', '.join(PROJECTS)}")

    from nw.config import settings
    from nw.platform.base import platform_for, tenant_from_env

    cfg = settings()
    if track and cfg.track.value != track:
        ap.error(f"NW_TRACK is {cfg.track.value}; this is the {track} command")
    registry = platform_for(cfg).registry
    tenant = tenant_from_env(cfg)
    code = 0
    for name in args.names:
        try:
            version, created = bootstrap(
                registry,
                tenant,
                name,
                root=args.root,
                approve=not args.no_approve,
                force=args.force,
            )
        except FileNotFoundError as exc:
            print(f"{name}: {exc}")
            code = 1
            continue
        verb = "registered" if created else "already has"
        print(f"{name}: {tenant.resource(name)} {verb} version {version.version} ({version.stage})")
    return code
