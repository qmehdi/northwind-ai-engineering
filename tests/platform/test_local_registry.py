"""The MLflow-backed model registry: register, stages by alias, live, download, promotion copy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nw.platform.base import ModelRegistry, Stage, Tenant
from nw.platform.local import LocalModelRegistry

pytestmark = pytest.mark.session06


def make_artifact(root: Path, version: str = "v1") -> Path:
    d = root / version
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({"version": version, "kind": "widget"}))
    (d / "weights.bin").write_bytes(b"\x00\x01\x02")
    return d


def test_register_creates_a_candidate_with_metrics_and_tags(mlflow_uri, tenant, tmp_path):
    registry = LocalModelRegistry(mlflow_uri)
    assert isinstance(registry, ModelRegistry)
    art = make_artifact(tmp_path / "art")
    v = registry.register(tenant, "widget", art, {"f1": 0.91}, {"git_sha": "abc123"})
    assert v.name == "widget" and v.version == "1" and v.stage is Stage.CANDIDATE
    assert v.uri == "models:/northwind-alice-widget/1"
    assert v.tags["git_sha"] == "abc123" and v.tags["artifact_version"] == "v1"
    assert v.tags["kind"] == "artifact"
    listed = registry.versions(tenant, "widget")
    assert [x.version for x in listed] == ["1"]
    assert listed[0].metrics["f1"] == pytest.approx(0.91)


def test_stages_move_by_alias_and_live_follows(mlflow_uri, tenant, tmp_path):
    registry = LocalModelRegistry(mlflow_uri)
    v1 = registry.register(tenant, "widget", make_artifact(tmp_path / "a", "v1"), {}, {})
    v2 = registry.register(tenant, "widget", make_artifact(tmp_path / "b", "v2"), {}, {})
    assert registry.live(tenant, "widget") is None
    moved = registry.set_stage(tenant, "widget", v1.version, Stage.LIVE, "gate passed")
    assert moved.stage is Stage.LIVE and moved.tags["stage_reason"] == "gate passed"
    assert registry.live(tenant, "widget").version == v1.version
    registry.set_stage(tenant, "widget", v2.version, Stage.LIVE, "next gate passed")
    assert registry.live(tenant, "widget").version == v2.version
    # the alias is unique: the previous live version is no longer live
    stages = {x.version: x.stage for x in registry.versions(tenant, "widget")}
    assert stages[v2.version] is Stage.LIVE and stages[v1.version] is not Stage.LIVE
    retired = registry.set_stage(tenant, "widget", v1.version, Stage.RETIRED, "superseded")
    assert retired.stage is Stage.RETIRED


def test_tenants_do_not_collide(mlflow_uri, tmp_path):
    registry = LocalModelRegistry(mlflow_uri)
    alice, bob = Tenant("alice"), Tenant("bob")
    registry.register(alice, "widget", make_artifact(tmp_path / "a"), {}, {})
    assert registry.versions(bob, "widget") == []
    assert registry.model_name(alice, "widget") != registry.model_name(bob, "widget")


def test_download_returns_the_artifact_directory(mlflow_uri, tenant, tmp_path):
    registry = LocalModelRegistry(mlflow_uri)
    art = make_artifact(tmp_path / "art")
    v = registry.register(tenant, "widget", art, {}, {})
    got = registry.download(tenant, v, tmp_path / "dl")
    assert (got / "weights.bin").read_bytes() == b"\x00\x01\x02"
    assert json.loads((got / "metadata.json").read_text())["version"] == "v1"


def test_copy_to_higher_environment_keeps_lineage(mlflow_uri, tenant, tmp_path):
    registry = LocalModelRegistry(mlflow_uri)
    v = registry.register(tenant, "widget", make_artifact(tmp_path / "art"), {"f1": 0.5}, {})
    registry.set_stage(tenant, "widget", v.version, Stage.LIVE, "gate")
    higher = Tenant(tenant.name, "higher")
    copied = registry.copy_to(tenant, "widget", registry.live(tenant, "widget"), higher)
    assert copied.uri.startswith("models:/higher-alice-widget/")
    assert copied.tags["promoted_from"] == v.uri and copied.stage is Stage.CANDIDATE
    assert registry.live(higher, "widget") is None
    registry.set_stage(higher, "widget", copied.version, Stage.LIVE, "promoted")
    assert registry.live(higher, "widget").version == copied.version


@pytest.mark.skipif(
    not Path("artifacts/triage/latest/model.joblib").exists(), reason="no trained triage artifact"
)
def test_triage_artifact_registers_as_a_serving_pyfunc(mlflow_uri, tenant, tmp_path):
    """The real Project 1 artifact loads through the pyfunc and scores a ticket."""
    import mlflow.pyfunc

    registry = LocalModelRegistry(mlflow_uri)
    artifact = Path(__file__).resolve().parents[2] / "artifacts" / "triage" / "latest"
    v = registry.register(tenant, "triage", artifact, {}, {})
    assert v.tags["kind"] == "triage"
    model = mlflow.pyfunc.load_model(v.uri)
    out = model.predict([{"subject": "URGENT production down", "body": "everything is down"}])
    assert out[0]["priority"] in {"P0", "P1", "P2", "P3"} and "probabilities" in out[0]
