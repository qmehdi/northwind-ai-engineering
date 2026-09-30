"""The champion a pipeline's gate compares against.

`champion=registry` (the pipelines' default): the version the tenant's registry holds as `live`,
downloaded and summarised by the project's own `summary()`, so the gate compares against what
is actually served. When nothing is live yet (the first model, or a tenant that never promoted)
the committed production summary stands in, and the gate's record says so. `champion=summary`:
the production summary file only, the behaviour of the laptop gate and of CI.

Which one decided is written into the gate result as `champion_source`
(`registry:<version>`, `summary:<path>` or `none`), and a registry that cannot be read fails the
step: a gate that silently lost its comparison is the failure the audit found.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nw.platform.base import ModelRegistry, Tenant

CHAMPIONS = ("registry", "summary")


def from_registry(
    pipeline: str,
    registry: ModelRegistry,
    tenant: Tenant,
    summarise: Callable[[Path], dict[str, Any]],
    *,
    name: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The live version's production summary and where it came from, or (None, reason)."""
    live = registry.live(tenant, name or pipeline)
    if live is None:
        return None, "registry: nothing live"
    with tempfile.TemporaryDirectory(prefix="nw-champion-") as tmp:
        root = Path(registry.download(tenant, live, Path(tmp)))
        found = sorted(root.rglob("metadata.json"), key=lambda p: len(p.parts))
        if not found:
            raise SystemExit(
                f"live version {live.version} of {tenant.resource(name or pipeline)} has no "
                "metadata.json: it cannot be the champion; bootstrap or promote a pipeline version"
            )
        summary = summarise(found[0].parent)
    return summary, f"registry:{live.version}"


def choose(
    pipeline: str,
    out: Path,
    production_summary: str | Path | None,
    champion: str,
    *,
    summarise: Callable[[Path], dict[str, Any]],
    default: Path,
    current: Callable[[Path, Path], dict[str, Any] | None],
    registry: ModelRegistry | None = None,
    tenant: Tenant | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The production summary a gate step compares against, and where it came from."""
    from nw.pipelines.steps import production_summary_path

    if champion not in CHAMPIONS:
        raise SystemExit(f"--champion {champion!r}: expected one of {', '.join(CHAMPIONS)}")
    note = ""
    if champion == "registry":
        registry = registry or registry_for_steps()
        tenant = tenant or tenant_for_steps()
        found, note = from_registry(pipeline, registry, tenant, summarise)
        if found is not None:
            return found, note
    path = production_summary_path(production_summary, default)
    suffix = f" ({note})" if note else ""
    if path is None:
        return None, "none" + suffix
    production = current(out, path)
    source = f"summary:{production_summary or path}" if production else "none"
    return production, source + suffix


def registry_for_steps() -> ModelRegistry:
    from nw.pipelines.steps.register import registry_from_env

    return registry_from_env()


def tenant_for_steps(tenant: str | None = None, environment: str | None = None) -> Tenant:
    from nw.pipelines.steps.register import tenant_from_args

    return tenant_from_args(tenant, environment)
