"""The per-cloud `bootstrap` command (mid-course recovery, audit 04 M21), lineage from the
environment when a container has no `.git` (audit 03 M2), and the Bedrock sync deadline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nw.platform import bootstrap as boot
from nw.platform import lineage


def test_git_sha_comes_from_the_environment_before_git_and_from_the_image_label_after():
    assert lineage.git_sha_and_source({"NW_GIT_SHA": "abc123def4567890"}) == (
        "abc123def456",
        "NW_GIT_SHA",
    )
    assert lineage.git_sha_and_source({"GITHUB_SHA": "fedcba9876543210"}) == (
        "fedcba987654",
        "GITHUB_SHA",
    )
    nowhere = Path("/")  # no checkout: git fails, the image label answers
    assert lineage.git_sha_and_source({"NW_IMAGE_GIT_SHA": "0badc0de"}, cwd=nowhere) == (
        "0badc0de",
        "image-label",
    )
    assert lineage.git_sha_and_source({}, cwd=nowhere) == ("nogit", "none")
    assert lineage.resolve("nogit", {"NW_GIT_SHA": "a1b2c3"}) == "a1b2c3"
    assert lineage.resolve("deadbeef", {"NW_GIT_SHA": "a1b2c3"}) == "deadbeef"


def test_lineage_records_the_lock_hash_and_the_image_digest(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    found = lineage.lineage(
        {"NW_GIT_SHA": "abc", "NW_IMAGE_DIGEST": "sha256:0f", "NW_SOURCE_SHA256_12": "s"}, lock
    )
    assert (
        found["uv_lock_sha256_12"] == lineage.lock_sha(lock)
        and len(found["uv_lock_sha256_12"]) == 12
    )
    assert found["image_digest"] == "sha256:0f" and found["source_sha256_12"] == "s"
    # an image has no uv.lock at hand: the build's hash stands in
    assert lineage.lock_sha(tmp_path / "missing", {"NW_UV_LOCK_SHA256_12": "c0ffee"}) == "c0ffee"


def test_bootstrap_tags_say_where_the_version_came_from(monkeypatch):
    monkeypatch.setenv("NW_GIT_SHA", "abc123")
    meta = {
        "version": "20260930-abc-308ad1151d5c",
        "data_sha256_12": "308ad1151d5c",
        "git_sha": "nogit",
        "metrics": {"test": {"macro_f1": 0.66, "p0_recall": 0.9, "extra": 1}, "p0_threshold": 0.05},
    }
    assert boot.metrics_of("triage", meta) == {
        "macro_f1": 0.66,
        "p0_recall": 0.9,
        "p0_threshold": 0.05,
    }
    tags = boot.tags_of("triage", meta)
    assert tags["source"] == "bootstrap" and tags["trigger"] == "bootstrap"
    assert tags["git_sha"] == "abc123" and tags["artifact_version"] == meta["version"]


def test_bootstrap_without_a_promoted_artifact_says_how_to_get_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="make train-semantic"):
        boot.artifact_dir("semantic", tmp_path)


@pytest.mark.parametrize("track", ["aws", "gcp", "azure"])
def test_every_cloud_cli_has_bootstrap(track, monkeypatch):
    import importlib

    seen: dict[str, object] = {}

    def fake(argv, *, track):
        seen.update(argv=list(argv), track=track)
        return 0

    monkeypatch.setattr(boot, "main", fake)
    module = importlib.import_module(f"nw.platform.{track}")
    assert module.main(["bootstrap", "triage", "--force"]) == 0
    assert seen == {"argv": ["triage", "--force"], "track": track}


def test_bootstrap_cli_refuses_the_wrong_track(monkeypatch):
    from nw.config import Settings, Track

    monkeypatch.setattr("nw.config.settings", lambda: Settings(_env_file=None, track=Track.LOCAL))
    with pytest.raises(SystemExit):
        boot.main(["triage"], track="aws")


def test_bootstrap_cli_registers_and_approves_on_the_track(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from nw.config import Settings, Track
    from tests.pipelines.fake_registry import FakeRegistry

    registry = FakeRegistry(tmp_path / "reg")
    root = tmp_path / "artifacts"
    art = root / "triage" / "v1"
    art.mkdir(parents=True)
    (art / "metadata.json").write_text(json.dumps({"version": "v1", "metrics": {"test": {}}}))
    (root / "triage" / "latest").symlink_to(art, target_is_directory=True)
    monkeypatch.setattr(
        "nw.config.settings", lambda: Settings(_env_file=None, track=Track.AWS, tenant="alice")
    )
    monkeypatch.setattr(
        "nw.platform.base.platform_for", lambda s: SimpleNamespace(registry=registry)
    )
    assert boot.main(["triage", "--root", str(root)], track="aws") == 0
    [v] = registry.versions(boot.Tenant("alice"), "triage")
    assert v.stage.value == "approved" and v.tags["source"] == "bootstrap"
    assert boot.main(["semantic", "--root", str(root)], track="aws") == 1, "nothing to register"


def test_knowledge_base_sync_has_a_deadline(monkeypatch):
    pytest.importorskip("boto3")
    from types import SimpleNamespace

    from nw.platform import aws
    from nw.platform.base import Tenant

    class Agent:
        def start_ingestion_job(self, **kw):
            return {"ingestionJob": {"ingestionJobId": "J1", "status": "IN_PROGRESS"}}

        def get_ingestion_job(self, **kw):
            return {"ingestionJob": {"ingestionJobId": "J1", "status": "IN_PROGRESS"}}

    cfg = aws.AwsPlatformConfig(
        region="us-east-1", knowledge_bases={"alice": "KB"}, data_sources={"alice": "DS"}
    )
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(aws.time, "monotonic", lambda: float(next(clock)))
    kb = aws.BedrockKnowledgeBase(
        Agent(), SimpleNamespace(), SimpleNamespace(), cfg, sleep=lambda s: None, timeout_s=250
    )
    with pytest.raises(TimeoutError, match="still IN_PROGRESS"):
        kb._sync(Tenant("alice"))


def test_aws_experiments_write_under_the_tenants_prefix(monkeypatch):
    """A tenant's role may write MLflow artifacts only under `mlflow/tenants/<tenant>/`."""
    from nw.triage import tracking

    monkeypatch.delenv("NW_MLFLOW_ARTIFACT_ROOT", raising=False)
    monkeypatch.setenv("NW_TRACK", "aws")
    monkeypatch.setenv("NW_TENANT", "alice")
    monkeypatch.setenv("NW_AWS_ARTIFACTS_BUCKET", "northwind-artifacts")
    assert tracking.artifact_location("northwind-alice-laptop-triage") == (
        "s3://northwind-artifacts/mlflow/tenants/alice/northwind-alice-laptop-triage"
    )
    monkeypatch.setenv("NW_TRACK", "local")
    assert tracking.artifact_location("triage") is None
    monkeypatch.setenv("NW_MLFLOW_ARTIFACT_ROOT", "s3://x/y/")
    assert tracking.artifact_location("triage") == "s3://x/y/triage"
