"""Durable operations state: trajectories, proposals, approvals, escalations and feedback.

On a laptop these are files under `artifacts/`. On a platform a container's disk is gone
with the container (`/tmp/traces` on Lambda lasts one warm instance), so the human loops,
approve, review and the golden-set feedback, would read nothing. One small interface
covers both: a store of keyed objects, local files on the Local track and the platform's
object storage on the cloud tracks, every key under the tenant's prefix.

    NW_OPS_STORE=s3://northwind-ops-123456789012/ops          # AWS, S3
    NW_OPS_STORE=gs://<artifacts bucket>                      # Google Cloud, Cloud Storage
    NW_OPS_STORE=https://nwops.blob.core.windows.net/ops      # Azure, Blob Storage
    (unset)                                                   # Local: the paths below

A cloud key is `<prefix>/<environment>-<tenant>/<kind>/<name>`: the environment from
`NW_ENVIRONMENT` (`northwind` when unset) and the tenant from `NW_TENANT` (`solo`), so a
learner's approvals never list another learner's proposals and a bucket policy or IAM
condition on the tenant prefix is all the isolation needs. The kinds are `trajectories`,
`approvals` (claim markers, approval records and the escalation queue) and `feedback`; the
infrastructure's retention rules are written against those prefixes.

Two operations are what make the state safe to act on, not just to read:
- `create(key, data)` writes only when the key does not exist (S3 `If-None-Match: *`,
  Cloud Storage `ifGenerationMatch=0`, Blob Storage `If-None-Match: *`, `open(..., "x")`
  locally). The approval claim marker is one: two operators approving the same proposal
  at the same moment get one execution and one refusal.
- `append(key, record)` adds a record: a line in a JSONL file locally, one object per
  record in the cloud (object storage has no append), read back in order by `records`.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._=-]{0,127})*$")
TENANT_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")


def safe_key(key: str) -> str:
    """A key the store accepts: path segments of letters, digits, dot, dash, underscore,
    never `..` and never absolute. Ids from requests reach keys, so this is the one place a
    `../` or a `/` in a ticket id is refused."""
    if not KEY_RE.match(key) or any(part in {".", ".."} for part in key.split("/")):
        raise ValueError(f"unsafe store key {key!r}")
    return key


class OpsStore(Protocol):
    location: str

    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def keys(self, prefix: str = "") -> list[str]: ...
    def create(self, key: str, data: bytes) -> bool: ...
    def append(self, key: str, record: dict[str, Any]) -> None: ...
    def records(self, key: str) -> list[dict[str, Any]]: ...


def _record_name() -> str:
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.json"


class LocalStore:
    """Files under a root directory. Appends are JSONL lines in one file, as before."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.location = str(self.root)

    def _path(self, key: str) -> Path:
        return self.root / safe_key(key)

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def keys(self, prefix: str = "") -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def create(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            return False
        return True

    def append(self, key: str, record: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def records(self, key: str) -> list[dict[str, Any]]:
        path = self._path(key)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


class _ObjectStore:
    """What the three object stores share: a prefix, and append as one object per record."""

    prefix: str
    location: str

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{safe_key(key)}" if self.prefix else safe_key(key)

    def _rel(self, full: str) -> str:
        return full[len(self.prefix) + 1 :] if self.prefix else full

    def append(self, key: str, record: dict[str, Any]) -> None:
        self.put(f"{key}/{_record_name()}", json.dumps(record, default=str).encode())

    def records(self, key: str) -> list[dict[str, Any]]:
        out = []
        for k in self.keys(f"{key}/"):
            try:
                out.append(json.loads(self.get(k)))
            except (ValueError, FileNotFoundError):
                continue
        return out

    def put(self, key: str, data: bytes) -> str: ...  # pragma: no cover
    def get(self, key: str) -> bytes: ...  # pragma: no cover
    def keys(self, prefix: str = "") -> list[str]: ...  # pragma: no cover


class S3Store(_ObjectStore):
    def __init__(self, bucket: str, prefix: str, *, client: Any = None) -> None:
        import boto3

        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.client = client or boto3.client("s3")
        self.location = f"s3://{bucket}/{self.prefix}"

    def put(self, key: str, data: bytes) -> str:
        full = self._full(key)
        self.client.put_object(Bucket=self.bucket, Key=full, Body=data)
        return f"s3://{self.bucket}/{full}"

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception:  # noqa: BLE001
            return False
        return True

    def keys(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self._full_prefix(prefix)}
        while True:
            r = self.client.list_objects_v2(**kw)
            out += [self._rel(o["Key"]) for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(out)
            kw["ContinuationToken"] = r["NextContinuationToken"]

    def _full_prefix(self, prefix: str) -> str:
        return f"{self.prefix}/{prefix}" if self.prefix else prefix

    def create(self, key: str, data: bytes) -> bool:
        """S3 conditional write: `If-None-Match: *` fails with 412 when the key exists."""
        from botocore.exceptions import ClientError

        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self._full(key), Body=data, IfNoneMatch="*"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}:
                return False
            raise
        return True


class GcsStore(_ObjectStore):
    def __init__(self, bucket: str, prefix: str, *, client: Any = None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.client = client
        self.bucket_name, self.prefix = bucket, prefix.strip("/")
        self.bucket = client.bucket(bucket)
        self.location = f"gs://{bucket}/{self.prefix}"

    def put(self, key: str, data: bytes) -> str:
        full = self._full(key)
        self.bucket.blob(full).upload_from_string(data)
        return f"gs://{self.bucket_name}/{full}"

    def get(self, key: str) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return self.bucket.blob(self._full(key)).download_as_bytes()
        except NotFound as exc:
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return bool(self.bucket.blob(self._full(key)).exists())

    def keys(self, prefix: str = "") -> list[str]:
        full = f"{self.prefix}/{prefix}" if self.prefix else prefix
        return sorted(
            self._rel(b.name) for b in self.client.list_blobs(self.bucket_name, prefix=full)
        )

    def create(self, key: str, data: bytes) -> bool:
        """`if_generation_match=0`: the write succeeds only when no live object has the name."""
        from google.api_core.exceptions import PreconditionFailed

        try:
            self.bucket.blob(self._full(key)).upload_from_string(data, if_generation_match=0)
        except PreconditionFailed:
            return False
        return True


class AzureBlobStore(_ObjectStore):
    def __init__(
        self, account_url: str, container: str, prefix: str, *, client: Any = None
    ) -> None:
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self.container = client.get_container_client(container)
        self.prefix = prefix.strip("/")
        self.location = f"{account_url.rstrip('/')}/{container}/{self.prefix}"

    def put(self, key: str, data: bytes) -> str:
        full = self._full(key)
        self.container.upload_blob(full, data, overwrite=True)
        return f"{self.location}/{key}"

    def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.container.download_blob(self._full(key)).readall()
        except ResourceNotFoundError as exc:
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return bool(self.container.get_blob_client(self._full(key)).exists())

    def keys(self, prefix: str = "") -> list[str]:
        full = f"{self.prefix}/{prefix}" if self.prefix else prefix
        return sorted(self._rel(b.name) for b in self.container.list_blobs(name_starts_with=full))

    def create(self, key: str, data: bytes) -> bool:
        """`overwrite=False` sends `If-None-Match: *`; an existing blob raises ResourceExists."""
        from azure.core.exceptions import ResourceExistsError

        try:
            self.container.upload_blob(self._full(key), data, overwrite=False)
        except ResourceExistsError:
            return False
        return True


def tenant(env: dict[str, str] | None = None) -> str:
    name = ((env if env is not None else os.environ).get("NW_TENANT") or "solo").strip()
    if not TENANT_RE.match(name):
        raise ValueError(f"NW_TENANT={name!r} is not a tenant name")
    return name


def tenant_prefix(env: dict[str, str] | None = None) -> str:
    """`<environment>-<tenant>`, the prefix every tenant-owned object sits under."""
    e = env if env is not None else os.environ
    environment = (e.get("NW_ENVIRONMENT") or "northwind").strip()
    if not re.match(r"^[a-z][a-z0-9-]{0,30}$", environment):
        raise ValueError(f"NW_ENVIRONMENT={environment!r} is not an environment name")
    return f"{environment}-{tenant(e)}"


def store_for(kind: str, *, local: Path, env: dict[str, str] | None = None) -> OpsStore:
    """The store for one kind of state (`trajectories`, `approvals`, `feedback`).

    With `NW_OPS_STORE` unset, the local directory `local`. With it set, the platform's object
    storage under `<prefix>/<environment>-<tenant>/<kind>`."""
    e = env if env is not None else dict(os.environ)
    uri = (e.get("NW_OPS_STORE") or "").strip()
    if not uri:
        return LocalStore(local)
    safe_key(kind)
    who = tenant_prefix(e)
    if uri.startswith("s3://"):
        bucket, _, prefix = uri[5:].partition("/")
        return S3Store(bucket, "/".join(p for p in (prefix.strip("/"), who, kind) if p))
    if uri.startswith("gs://"):
        bucket, _, prefix = uri[5:].partition("/")
        return GcsStore(bucket, "/".join(p for p in (prefix.strip("/"), who, kind) if p))
    m = re.match(r"^(https://[^/]+\.blob\.core\.windows\.net)/([^/]+)/?(.*)$", uri)
    if m:
        account_url, container, prefix = m.groups()
        return AzureBlobStore(
            account_url, container, "/".join(p for p in (prefix.strip("/"), who, kind) if p)
        )
    if "://" in uri:
        raise ValueError(f"NW_OPS_STORE={uri!r}: expected s3://, gs:// or an Azure blob URL")
    return LocalStore(Path(uri) / who / kind)
