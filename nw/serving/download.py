"""The model artifact a service serves, from wherever the platform put it.

The platform injects `NW_MODEL_URI` (and `NW_MODEL_VERSION`) into a deployed service: the
artifact the registry holds for the approved version (ADR 0008). The URI is one of:

- `s3://bucket/prefix/` or `s3://bucket/prefix/model.tar.gz` (SageMaker Model Registry)
- `gs://bucket/prefix/` (Vertex Model Registry; the Agent Platform also sets `AIP_STORAGE_URI`
  to the same value on a custom container, which is honoured when `NW_MODEL_URI` is unset)
- `models:/name/version` or `models:/name@alias` (the MLflow registry on the Local track)
- `file:///path/to/version-dir` or `file:///path/to/model.tar.gz` (tests, and a laptop)

`model_source` fetches it into a fresh temp directory, unpacks a tarball when it finds one,
locates the directory that holds `metadata.json`, and reports the version: `NW_MODEL_VERSION`
when the platform set it, else the artifact's own. Without a URI it points at the fallback path
(`artifacts/<project>/latest`), which is how the guide's laptop steps and the images keep
working. A fetch that fails never raises here: the source records the error and points at an
empty directory, so the service's own load fails with a clear message, stays alive, and reports
not ready (the pattern Project 1 taught).

The cloud SDKs and MLflow are imported inside the fetch that needs them, so a service on one
track never needs the other tracks' packages installed.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nw.logging import get_logger, log_fields

log = get_logger("nw.serving.download")

URI_VAR = "NW_MODEL_URI"
VERSION_VAR = "NW_MODEL_VERSION"
# Set by the Agent Platform on a custom container deployed from a registered model with an
# artifact URI (custom container requirements, docs.cloud.google.com, fetched 2026-09-29).
AIP_STORAGE_URI = "AIP_STORAGE_URI"
SCHEMES = ("s3://", "gs://", "file://", "models:/", "runs:/", "mlflow-artifacts:/")
TARBALL_SUFFIXES = (".tar.gz", ".tgz")
METADATA = "metadata.json"


@dataclass(frozen=True)
class ModelSource:
    """Where the served artifact came from and where it now is."""

    path: Path  # the directory to load; holds metadata.json when the fetch worked
    uri: str | None  # the URI the platform gave, None for the fallback path
    version: str | None  # NW_MODEL_VERSION, else the artifact's metadata version
    fetched: bool = False  # True when the files were materialised into a temp directory
    error: str | None = None  # why a fetch failed; the path is then an empty directory

    def as_dict(self) -> dict[str, object]:
        return {
            "model_uri": self.uri,
            "model_version": self.version,
            "path": str(self.path),
            "fetched": self.fetched,
            "error": self.error,
        }


def is_uri(value: str | None) -> bool:
    return bool(value) and str(value).startswith(SCHEMES)


def artifact_version(path: Path) -> str | None:
    meta = Path(path) / METADATA
    if not meta.is_file():
        return None
    try:
        version = json.loads(meta.read_text(encoding="utf-8")).get("version")
    except (ValueError, OSError):
        return None
    return str(version) if version is not None else None


def model_source(
    *,
    fallback: Path,
    env: Mapping[str, str] | None = None,
    into: Path | None = None,
    uri_var: str = URI_VAR,
    version_var: str = VERSION_VAR,
) -> ModelSource:
    """The artifact a service should load. `into` is the temp directory to fetch into; a fresh
    one is created when it is not given."""
    e = os.environ if env is None else env
    uri = (e.get(uri_var) or e.get(AIP_STORAGE_URI) or "").strip() or None
    pinned = (e.get(version_var) or "").strip() or None
    if uri is None:
        path = Path(fallback)
        return ModelSource(path=path, uri=None, version=pinned or artifact_version(path))
    if not is_uri(uri):
        # A plain path in the URI variable: serve it in place, the way the fallback is served.
        path = Path(uri)
        return ModelSource(path=path, uri=uri, version=pinned or artifact_version(path))
    target = Path(into) if into is not None else Path(tempfile.mkdtemp(prefix="nw-model-"))
    target.mkdir(parents=True, exist_ok=True)
    try:
        path = fetch(uri, target)
    except Exception as exc:  # noqa: BLE001  the service reports not ready, never crashes
        log.error("model fetch failed", extra=log_fields(model_uri=uri, error=str(exc)))
        return ModelSource(path=target, uri=uri, version=pinned, fetched=False, error=str(exc))
    version = pinned or artifact_version(path)
    log.info(
        "model fetched",
        extra=log_fields(model_uri=uri, model_version=version, path=str(path)),
    )
    return ModelSource(path=path, uri=uri, version=version, fetched=True)


def fetch(uri: str, into: Path) -> Path:
    """Materialise `uri` under `into` and return the directory that holds the artifact."""
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    if uri.startswith("s3://"):
        _fetch_s3(uri, into)
    elif uri.startswith("gs://"):
        _fetch_gcs(uri, into)
    elif uri.startswith("file://"):
        _fetch_file(uri, into)
    elif uri.startswith(("models:/", "runs:/", "mlflow-artifacts:/")):
        return _locate(_fetch_mlflow(uri, into))
    else:
        raise ValueError(f"unsupported model uri {uri!r}; expected one of {SCHEMES}")
    return _locate(into)


# ----- the fetches ---------------------------------------------------------------------


def _fetch_file(uri: str, into: Path) -> None:
    src = Path(uri.removeprefix("file://"))
    if not src.exists():
        raise FileNotFoundError(f"{src} does not exist")
    if src.is_dir():
        shutil.copytree(src, into, dirs_exist_ok=True)
    else:
        shutil.copy2(src, into / src.name)


def _fetch_s3(uri: str, into: Path) -> None:
    import boto3

    bucket, _, key = uri.removeprefix("s3://").partition("/")
    client = boto3.client("s3", region_name=os.environ.get("NW_AWS_REGION", "us-east-1"))
    if key.endswith(TARBALL_SUFFIXES) or "." in key.rsplit("/", 1)[-1]:
        client.download_file(bucket, key, str(into / key.rsplit("/", 1)[-1]))
        return
    prefix = key if key.endswith("/") or not key else key + "/"
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    found = 0
    for page in pages:
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :]
            if not rel or rel.endswith("/"):
                continue
            dest = into / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, obj["Key"], str(dest))
            found += 1
    if not found:
        raise FileNotFoundError(f"nothing under {uri}")


def _fetch_gcs(uri: str, into: Path) -> None:
    from google.cloud import storage

    bucket_name, _, key = uri.removeprefix("gs://").partition("/")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    if key.endswith(TARBALL_SUFFIXES) or "." in key.rsplit("/", 1)[-1]:
        bucket.blob(key).download_to_filename(str(into / key.rsplit("/", 1)[-1]))
        return
    prefix = key if key.endswith("/") or not key else key + "/"
    found = 0
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        rel = blob.name[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        dest = into / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))
        found += 1
    if not found:
        raise FileNotFoundError(f"nothing under {uri}")


def _fetch_mlflow(uri: str, into: Path) -> Path:
    import mlflow
    from mlflow.artifacts import download_artifacts

    tracking = os.environ.get("NW_MLFLOW_URI") or os.environ.get("MLFLOW_TRACKING_URI")
    if tracking:
        mlflow.set_tracking_uri(tracking)
    root = Path(download_artifacts(artifact_uri=uri, dst_path=str(into)))
    # The Local registry logs a pyfunc that keeps the artifact directory under artifacts/<name>.
    holder = root / "artifacts"
    inner = [p for p in holder.iterdir() if p.is_dir()] if holder.is_dir() else []
    return inner[0] if len(inner) == 1 else root


# ----- after the fetch ------------------------------------------------------------------


def _locate(root: Path) -> Path:
    """Unpack any tarball at the top of `root`, then find the directory holding metadata.json:
    the root itself, else the one directory under it that has one."""
    root = Path(root)
    for tar_path in sorted(p for p in root.iterdir() if p.name.endswith(TARBALL_SUFFIXES)):
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(root, filter="data")
        tar_path.unlink()
    if (root / METADATA).is_file():
        return root
    holders = sorted({p.parent for p in root.rglob(METADATA)}, key=lambda p: len(p.parts))
    if holders:
        return holders[0]
    raise FileNotFoundError(f"no {METADATA} under {root}: not a course artifact")
