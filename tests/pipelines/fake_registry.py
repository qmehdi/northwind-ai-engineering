"""A file-backed `ModelRegistry` for the pipeline tests: no platform, no network.

`NW_PIPELINE_REGISTRY=tests.pipelines.fake_registry:build` makes the register step use it in
a subprocess; `NW_FAKE_REGISTRY_DIR` says where it keeps its records, so the test that ran
the pipeline can read back what was registered.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from nw.platform.base import ModelVersion, Stage, Tenant


class FakeRegistry:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, tenant: Tenant, name: str) -> Path:
        return self.root / f"{tenant.resource(name)}.json"

    def _load(self, tenant: Tenant, name: str) -> list[dict]:
        path = self._file(tenant, name)
        return json.loads(path.read_text()) if path.exists() else []

    def _save(self, tenant: Tenant, name: str, rows: list[dict]) -> None:
        self._file(tenant, name).write_text(json.dumps(rows, indent=1))

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
    ) -> ModelVersion:
        rows = self._load(tenant, name)
        version = ModelVersion(
            name=tenant.resource(name),
            version=str(len(rows) + 1),
            stage=Stage.CANDIDATE,
            uri=str(Path(artifact).resolve()),
            metrics=dict(metrics),
            tags=dict(tags),
        )
        rows.append(version.__dict__ | {"stage": version.stage.value})
        self._save(tenant, name, rows)
        return version

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        rows = self._load(tenant, name)
        for row in rows:
            if row["version"] == version:
                row["stage"] = stage.value
                row["tags"]["reason"] = reason
                self._save(tenant, name, rows)
                return ModelVersion(**row | {"stage": Stage(row["stage"])})
        raise KeyError(version)

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        return [
            ModelVersion(**row | {"stage": Stage(row["stage"])}) for row in self._load(tenant, name)
        ]

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        return next((v for v in self.versions(tenant, name) if v.stage == Stage.LIVE), None)

    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path:
        shutil.copytree(version.uri, into, dirs_exist_ok=True)
        return Path(into)


def build() -> FakeRegistry:
    return FakeRegistry(Path(os.environ["NW_FAKE_REGISTRY_DIR"]))
