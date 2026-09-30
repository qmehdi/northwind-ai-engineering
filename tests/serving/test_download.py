"""The model artifact from NW_MODEL_URI: file:// directories and tarballs, the fallback, the
pinned version, and a failed fetch that never raises."""

import json
import tarfile
from pathlib import Path

import pytest

from nw.serving.download import ModelSource, artifact_version, fetch, is_uri, model_source


def _artifact(root: Path, version: str = "v-artifact") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(json.dumps({"version": version}))
    (root / "model.joblib").write_bytes(b"not really a model")
    return root


def test_file_directory_is_fetched_into_a_temp_dir(tmp_path):
    src = _artifact(tmp_path / "src")
    into = tmp_path / "into"
    source = model_source(fallback=Path("unused"), env={"NW_MODEL_URI": src.as_uri()}, into=into)
    assert source.fetched and source.error is None
    assert source.path == into and (into / "model.joblib").read_bytes() == b"not really a model"
    assert source.version == "v-artifact" and source.uri == src.as_uri()


def test_tarball_is_unpacked_and_metadata_located(tmp_path):
    src = _artifact(tmp_path / "src")
    tar_path = tmp_path / "model.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for p in src.iterdir():
            tar.add(p, arcname=p.name)
    into = tmp_path / "into"
    found = fetch(tar_path.as_uri(), into)
    assert found == into and (into / "metadata.json").exists()
    assert not (into / "model.tar.gz").exists()  # unpacked, then removed


def test_nested_directory_inside_the_tarball_is_found(tmp_path):
    src = _artifact(tmp_path / "src" / "20260929-1200")
    tar_path = tmp_path / "model.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(src, arcname="20260929-1200")
    found = fetch(tar_path.as_uri(), tmp_path / "into")
    assert found.name == "20260929-1200" and artifact_version(found) == "v-artifact"


def test_pinned_version_wins_over_the_artifact(tmp_path):
    src = _artifact(tmp_path / "src")
    source = model_source(
        fallback=Path("unused"),
        env={"NW_MODEL_URI": src.as_uri(), "NW_MODEL_VERSION": "7"},
        into=tmp_path / "into",
    )
    assert source.version == "7" and source.as_dict()["model_uri"] == src.as_uri()


def test_fallback_when_no_uri(tmp_path):
    fallback = _artifact(tmp_path / "latest", "v-local")
    source = model_source(fallback=fallback, env={})
    assert source == ModelSource(path=fallback, uri=None, version="v-local")
    assert not source.fetched


def test_plain_path_in_the_variable_is_served_in_place(tmp_path):
    local = _artifact(tmp_path / "plain")
    source = model_source(fallback=Path("unused"), env={"NW_MODEL_URI": str(local)})
    assert source.path == local and not source.fetched and source.version == "v-artifact"


def test_aip_storage_uri_is_honoured_when_model_uri_is_unset(tmp_path):
    src = _artifact(tmp_path / "src")
    source = model_source(
        fallback=Path("unused"), env={"AIP_STORAGE_URI": src.as_uri()}, into=tmp_path / "into"
    )
    assert source.fetched and source.uri == src.as_uri()


def test_failed_fetch_records_the_error_and_points_at_an_empty_dir(tmp_path):
    missing = (tmp_path / "nowhere").as_uri()
    source = model_source(
        fallback=Path("unused"), env={"NW_MODEL_URI": missing}, into=tmp_path / "into"
    )
    assert source.error and not source.fetched and source.version is None
    assert source.path.is_dir() and not any(source.path.iterdir())


def test_unknown_scheme_is_refused_by_fetch(tmp_path):
    with pytest.raises(ValueError):
        fetch("ftp://x/y", tmp_path)
    assert is_uri("s3://b/k") and is_uri("models:/m/1") and not is_uri("artifacts/triage")


class _FakeS3:
    """A bucket whose listing a hostile writer controls."""

    def __init__(self, keys):
        self.keys = keys
        self.written = []

    def get_paginator(self, name):
        keys = self.keys

        class _P:
            def paginate(self, **kw):
                return [{"Contents": [{"Key": k} for k in keys]}]

        return _P()

    def download_file(self, bucket, key, dest):
        self.written.append(dest)
        Path(dest).write_text("{}")


def _with_fake_boto3(monkeypatch, s3):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: s3))


@pytest.mark.parametrize(
    "bad_key", ["models/v1/../../escaped.json", "models/v1/../../../tmp/x", "models/v1//etc/x"]
)
def test_a_key_outside_the_target_is_refused(tmp_path, monkeypatch, bad_key):
    s3 = _FakeS3(["models/v1/metadata.json", bad_key])
    _with_fake_boto3(monkeypatch, s3)
    target = tmp_path / "into"
    with pytest.raises(ValueError, match="refusing object key"):
        fetch("s3://bucket/models/v1/", target)
    assert all(Path(p).resolve().is_relative_to(target.resolve()) for p in s3.written)
    assert not (tmp_path / "escaped.json").exists()


def test_the_registry_hash_travels_on_the_source(tmp_path):
    art = _artifact(tmp_path / "a")
    src = model_source(
        fallback=tmp_path / "none",
        env={"NW_MODEL_URI": f"file://{art}", "NW_MODEL_SHA256": "ABC123"},
        into=tmp_path / "into",
    )
    assert src.fetched and src.expected_sha256 == "abc123"
