"""One platform contract, one meaning: the stage rules and the prompt rules of
`nw.platform.base`, held against all four implementations with their fakes (audit 01 H12).

AWS runs on stateful fakes of SageMaker, S3 and Bedrock (`aws_fakes.py`), Google Cloud and
Azure on the fakes of their own test modules, Local on MLflow over a temporary sqlite file.
A rule that holds on three tracks and not the fourth fails here, not in a learner's capstone."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nw.platform.base import (
    ModelRegistry,
    PromptStore,
    Stage,
    Tenant,
    default_prompt,
    promotion_candidate,
)

TRACKS = ["aws", "gcp", "azure", "local"]


def _aws(tmp_path: Path) -> SimpleNamespace:
    pytest.importorskip("boto3")
    from nw.platform import aws
    from tests.platform.aws_fakes import ACCOUNT, REGION, FakeBedrockAgent, FakeS3, FakeSageMaker

    cfg = aws.AwsPlatformConfig(
        region=REGION,
        artifacts_bucket="northwind-artifacts",
        serving_role_arn=f"arn:aws:iam::{ACCOUNT}:role/northwind-serving",
        images={"triage": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/nw-triage:test"},
    )
    return SimpleNamespace(
        registry=aws.SageMakerRegistry(FakeSageMaker(), FakeS3(), cfg),
        prompts=aws.BedrockPrompts(FakeBedrockAgent(), cfg),
    )


def _gcp(tmp_path: Path) -> SimpleNamespace:
    from nw.platform import gcp
    from tests.platform.test_gcp_platform import FakeAiplatform, FakePrompts, FakeStorage

    cfg = gcp.GcpConfig(project="p", artifacts_bucket="arts", pipelines_bucket="pipes")
    clients = gcp.GcpClients(cfg).set(
        storage=FakeStorage(), aiplatform=FakeAiplatform(), prompts=FakePrompts()
    )
    return SimpleNamespace(
        registry=gcp.VertexModelRegistry(cfg, clients),
        prompts=gcp.VertexPromptStore(cfg, clients),
    )


def _azure(tmp_path: Path) -> SimpleNamespace:
    pytest.importorskip("azure.ai.ml")
    from nw.config import Settings, Track
    from nw.platform import azure
    from tests.platform.test_azure_platform import (
        OUTPUTS,
        FakeBlobService,
        FakeCredential,
        FakeML,
    )

    cfg = azure.AzureConfig.from_settings(
        Settings(_env_file=None, track=Track.AZURE), env={}, outputs=OUTPUTS
    )
    clients = azure.AzureClients(cfg).set(
        ml=FakeML(), blob=FakeBlobService(), credential=FakeCredential()
    )
    return SimpleNamespace(
        registry=azure.AzureMLRegistry(cfg, clients),
        prompts=azure.AzurePromptStore(cfg, clients),
    )


def _local(tmp_path: Path, mlflow_uri: str) -> SimpleNamespace:
    from nw.platform.local import LocalModelRegistry, LocalPromptStore

    return SimpleNamespace(
        registry=LocalModelRegistry(mlflow_uri), prompts=LocalPromptStore(mlflow_uri)
    )


@pytest.fixture(params=TRACKS)
def track(request, tmp_path: Path) -> SimpleNamespace:
    if request.param == "local":
        mlflow_uri = request.getfixturevalue("mlflow_uri")
        made = _local(tmp_path, mlflow_uri)
    else:
        made = {"aws": _aws, "gcp": _gcp, "azure": _azure}[request.param](tmp_path)
    made.name = request.param
    return made


@pytest.fixture
def alice() -> Tenant:
    return Tenant("alice", "northwind")


def _artifact(root: Path, n: int) -> Path:
    d = root / f"art{n}"
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "version": f"v{n}",
                "data_sha256_12": "abc",
                "git_sha": "nogit",
                "metrics": {"test": {"macro_f1": 0.6 + n / 100, "p0_recall": 0.9}},
            }
        )
    )
    (d / "weights.bin").write_bytes(b"w" * n)
    return d


def _register(track: SimpleNamespace, alice: Tenant, tmp_path: Path, n: int) -> str:
    v = track.registry.register(
        alice, "triage", _artifact(tmp_path, n), {"macro_f1": 0.6 + n / 100}, {"source": "test"}
    )
    assert v.stage == Stage.CANDIDATE
    return v.version


def _stages(track: SimpleNamespace, alice: Tenant) -> list[Stage]:
    return [v.stage for v in track.registry.versions(alice, "triage")]


# ----- the model registry ------------------------------------------------------------------


def test_versions_are_oldest_first_and_start_as_candidates(track, alice, tmp_path):
    assert isinstance(track.registry, ModelRegistry)
    assert list(track.registry.versions(alice, "triage")) == []
    ids = [_register(track, alice, tmp_path, n) for n in (1, 2, 3)]
    listed = track.registry.versions(alice, "triage")
    assert [v.version for v in listed] == ids, "oldest first: [-1] is the newest registration"
    assert _stages(track, alice) == [Stage.CANDIDATE] * 3
    assert track.registry.live(alice, "triage") is None


def test_one_approved_version_and_the_previous_steps_back_to_candidate(track, alice, tmp_path):
    v1, v2 = (_register(track, alice, tmp_path, n) for n in (1, 2))
    out = track.registry.set_stage(alice, "triage", v1, Stage.APPROVED, "read by a person")
    assert out.stage == Stage.APPROVED and out.version == v1
    track.registry.set_stage(alice, "triage", v2, Stage.APPROVED, "a better one")
    assert _stages(track, alice) == [Stage.CANDIDATE, Stage.APPROVED]


def test_one_live_version_the_previous_retires_and_live_never_means_approved(
    track, alice, tmp_path
):
    v1, v2, v3 = (_register(track, alice, tmp_path, n) for n in (1, 2, 3))
    track.registry.set_stage(alice, "triage", v3, Stage.APPROVED, "approved only")
    assert track.registry.live(alice, "triage") is None, "live() never falls back to approved"
    track.registry.set_stage(alice, "triage", v1, Stage.LIVE, "first release")
    assert track.registry.live(alice, "triage").version == v1
    track.registry.set_stage(alice, "triage", v2, Stage.LIVE, "canary passed")
    assert _stages(track, alice) == [Stage.RETIRED, Stage.LIVE, Stage.APPROVED]
    assert track.registry.live(alice, "triage").version == v2
    # rolling back is making the old version live again; the replaced one retires
    track.registry.set_stage(alice, "triage", v1, Stage.LIVE, "rollback")
    assert _stages(track, alice) == [Stage.LIVE, Stage.RETIRED, Stage.APPROVED]


def test_going_live_takes_a_version_out_of_approved(track, alice, tmp_path):
    v1 = _register(track, alice, tmp_path, 1)
    track.registry.set_stage(alice, "triage", v1, Stage.APPROVED, "approved")
    track.registry.set_stage(alice, "triage", v1, Stage.LIVE, "promoted")
    assert _stages(track, alice) == [Stage.LIVE]
    track.registry.set_stage(alice, "triage", v1, Stage.RETIRED, "withdrawn")
    assert _stages(track, alice) == [Stage.RETIRED] and track.registry.live(alice, "triage") is None


def test_promotion_candidate_is_the_approved_version_else_the_newest_candidate(
    track, alice, tmp_path
):
    """The capstone snippet: a learner who approved in Project 1 promotes that version; one who
    did not gets the candidate to approve; an empty registry says how to recover (04 H3)."""
    with pytest.raises(LookupError, match="bootstrap"):
        promotion_candidate(track.registry, alice, "triage")
    v1, v2 = (_register(track, alice, tmp_path, n) for n in (1, 2))
    assert promotion_candidate(track.registry, alice, "triage").version == v2
    track.registry.set_stage(alice, "triage", v1, Stage.APPROVED, "Project 1")
    assert promotion_candidate(track.registry, alice, "triage").version == v1


def test_bootstrap_registers_and_approves_the_promoted_artifact_once(track, alice, tmp_path):
    from nw.platform.bootstrap import bootstrap

    root = tmp_path / "artifacts"
    latest = root / "triage" / "latest"
    latest.parent.mkdir(parents=True)
    latest.symlink_to(_artifact(tmp_path, 7), target_is_directory=True)
    version, created = bootstrap(track.registry, alice, "triage", root=root)
    assert created and version.stage == Stage.APPROVED
    listed = track.registry.versions(alice, "triage")
    assert [v.stage for v in listed] == [Stage.APPROVED]
    assert listed[0].metrics.get("macro_f1") == pytest.approx(0.67)
    again, created = bootstrap(track.registry, alice, "triage", root=root)
    assert not created and again.version == version.version, "an approved tenant is left alone"
    assert promotion_candidate(track.registry, alice, "triage").version == version.version


# ----- the prompt store ----------------------------------------------------------------------


def test_prompts_are_idempotent_ordered_and_follow_the_stage_rules(track, alice):
    store: Any = track.prompts
    assert isinstance(store, PromptStore)
    a = store.register(alice, "policy.answer", "Answer from the policy.", {"owner": "p"})
    again = store.register(alice, "policy.answer", "Answer from the policy.", {"owner": "p"})
    assert again.version == a.version and again.sha256_12 == a.sha256_12, "same text, same version"
    b = store.register(alice, "policy.answer", "Answer from the policy, and cite it.", {})
    assert [v.version for v in store.versions(alice, "policy.answer")] == [a.version, b.version]
    assert store.get(alice, "policy.answer").version == b.version, "nothing staged: the newest"
    store.set_stage(alice, "policy.answer", a.version, Stage.APPROVED)
    assert store.get(alice, "policy.answer").version == a.version, "approved beats newer"
    store.set_stage(alice, "policy.answer", b.version, Stage.APPROVED)
    stages = {v.version: v.stage for v in store.versions(alice, "policy.answer")}
    assert stages == {a.version: Stage.CANDIDATE, b.version: Stage.APPROVED}
    store.set_stage(alice, "policy.answer", a.version, Stage.LIVE)
    assert store.get(alice, "policy.answer").version == a.version, "live beats approved"
    store.set_stage(alice, "policy.answer", b.version, Stage.LIVE)
    stages = {v.version: v.stage for v in store.versions(alice, "policy.answer")}
    assert stages == {a.version: Stage.RETIRED, b.version: Stage.LIVE}
    assert store.get(alice, "policy.answer", a.version).text == "Answer from the policy."
    assert default_prompt(store.versions(alice, "policy.answer")).version == b.version
