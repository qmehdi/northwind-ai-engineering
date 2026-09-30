"""Operations state goes through one store: files on the Local track, the platform's object
storage on the cloud tracks, under the tenant's prefix. The exclusive create is what makes
an approval claim safe; unsafe keys are refused."""

import io

import pytest
from botocore.exceptions import ClientError

from nw.agent import opstore
from nw.agent.opstore import AzureBlobStore, GcsStore, LocalStore, S3Store, safe_key, store_for
from nw.agent.trace import Trajectory

pytestmark = pytest.mark.session05


def test_local_store_create_is_exclusive_and_append_reads_back(tmp_path):
    s = LocalStore(tmp_path)
    assert s.create("claims/a.json", b"1") and not s.create("claims/a.json", b"2")
    assert s.get("claims/a.json") == b"1"
    s.append("feedback.jsonl", {"n": 1})
    s.append("feedback.jsonl", {"n": 2})
    assert [r["n"] for r in s.records("feedback.jsonl")] == [1, 2]
    assert s.keys("claims/") == ["claims/a.json"]


@pytest.mark.parametrize("key", ["../x", "/etc/passwd", "a/../b", "a b", "", "a//b"])
def test_unsafe_keys_are_refused(key):
    with pytest.raises(ValueError):
        safe_key(key)


class FakeS3:
    class exceptions:  # noqa: N801
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):  # noqa: N803
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)

    def list_objects_v2(self, Bucket, Prefix, **kw):  # noqa: N803
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def test_s3_store_claims_with_if_none_match_under_the_tenant_prefix():
    fake = FakeS3()
    s = S3Store("ops-bucket", "ops/alice/approvals", client=fake)
    assert s.create("claims/r1-s1-escalate.json", b"x")
    assert not s.create("claims/r1-s1-escalate.json", b"y")
    assert "ops/alice/approvals/claims/r1-s1-escalate.json" in fake.objects
    s.append("verdicts", {"v": 1})
    s.append("verdicts", {"v": 2})
    assert [r["v"] for r in s.records("verdicts")] == [1, 2]
    with pytest.raises(FileNotFoundError):
        s.get("missing.json")


class _Blob:
    def __init__(self, store: dict, name: str) -> None:
        self.store, self.name = store, name

    def upload_from_string(self, data, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed

        if if_generation_match == 0 and self.name in self.store:
            raise PreconditionFailed("exists")
        self.store[self.name] = data

    def download_as_bytes(self):
        return self.store[self.name]

    def exists(self):
        return self.name in self.store


class FakeGcs:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def bucket(self, name):
        outer = self

        class B:
            def blob(self, n):
                return _Blob(outer.objects, n)

        return B()

    def list_blobs(self, bucket, prefix):
        return [_Blob(self.objects, k) for k in self.objects if k.startswith(prefix)]


def test_gcs_store_claims_with_generation_zero():
    s = GcsStore("ops", "ops/alice/approvals", client=FakeGcs())
    assert s.create("claims/a.json", b"x") and not s.create("claims/a.json", b"y")
    assert s.keys("claims/") == ["claims/a.json"]


class FakeContainer:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_blob(self, name, data, overwrite=False):
        from azure.core.exceptions import ResourceExistsError

        if not overwrite and name in self.objects:
            raise ResourceExistsError("exists")
        self.objects[name] = data


class FakeBlobService:
    def __init__(self) -> None:
        self.container = FakeContainer()

    def get_container_client(self, name):
        return self.container


def test_azure_store_claims_without_overwrite():
    s = AzureBlobStore(
        "https://nwops.blob.core.windows.net", "ops", "alice/x", client=FakeBlobService()
    )
    assert s.create("claims/a.json", b"x") and not s.create("claims/a.json", b"y")


def test_store_for_picks_the_backend_and_keys_by_tenant(tmp_path, monkeypatch):
    local = store_for("traces", local=tmp_path, env={})
    assert isinstance(local, LocalStore) and local.root == tmp_path
    made: dict = {}
    monkeypatch.setattr(opstore, "S3Store", lambda b, p: made.setdefault("s3", (b, p)))
    monkeypatch.setattr(opstore, "GcsStore", lambda b, p: made.setdefault("gcs", (b, p)))
    store_for(
        "trajectories", local=tmp_path, env={"NW_OPS_STORE": "s3://b/ops", "NW_TENANT": "alice"}
    )
    store_for("trajectories", local=tmp_path, env={"NW_OPS_STORE": "gs://g"})
    assert made == {
        "s3": ("b", "ops/northwind-alice/trajectories"),
        "gcs": ("g", "northwind-solo/trajectories"),
    }
    with pytest.raises(ValueError):
        store_for("traces", local=tmp_path, env={"NW_OPS_STORE": "s3://b", "NW_TENANT": "../x"})


def test_a_trajectory_saves_to_a_store_redacted(tmp_path):
    s = LocalStore(tmp_path)
    t = Trajectory(run_id="r1", agent="resolver", task="mail anna@example.org please")
    t.save(s)
    back = Trajectory.load_from(s, "r1")
    assert "anna@example.org" not in back.task and "[EMAIL_1]" in back.task
