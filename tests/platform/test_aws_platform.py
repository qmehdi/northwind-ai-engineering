"""The AWS platform client against botocore's Stubber: every protocol method, no network.

Each stub asserts the exact request the client sends (names, statuses, payload shapes) and
answers with the documented response shape, so a change in the client's calls or in the SDK's
model fails here before it fails in an account."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import ANY, Stubber  # noqa: E402

from nw.platform import aws as platform  # noqa: E402
from nw.platform.base import (  # noqa: E402
    AgentRuntime,
    EndpointClient,
    ModelRegistry,
    ModelVersion,
    PipelineRun,
    PipelineRunner,
    PromptStore,
    RunStatus,
    Stage,
    Tenant,
    VectorStore,
)

REGION = "us-east-1"
ACCOUNT = "123456789012"
PKG_ARN = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:model-package/northwind-alice-triage/3"
LIVE_PKG_ARN = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:model-package/northwind-live-triage/1"
EXEC_ARN = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:pipeline/northwind-alice-triage/execution/abc123"
KB_ID = "KB12345678"
DS_ID = "DS12345678"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/northwind_alice_resolver-abc"
RECORD_ARN = f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/REG123456789/record/REC1234567890"
REGISTRY_ARN = f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/REG123456789"
RECORD_SUMMARY = {
    "registryArn": REGISTRY_ARN,
    "recordArn": RECORD_ARN,
    "recordId": "REC1234567890",
    "name": "northwind-alice-resolver",
    "recordType": "AGENT",
    "recordVersion": "1.2.0",
    "status": "DRAFT",
    "createdAt": "2026-09-29T00:00:00Z",
    "updatedAt": "2026-09-29T00:00:00Z",
}
SUBMITTED = {
    "registryArn": REGISTRY_ARN,
    "recordArn": RECORD_ARN,
    "recordId": "REC1234567890",
    "status": "PENDING_APPROVAL",
    "updatedAt": "2026-09-29T00:00:00Z",
}


def client(name: str):
    return boto3.client(
        name, region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )


@pytest.fixture
def cfg() -> platform.AwsPlatformConfig:
    return platform.AwsPlatformConfig(
        region=REGION,
        environment="northwind",
        data_bucket="northwind-data",
        artifacts_bucket="northwind-artifacts",
        serving_role_arn=f"arn:aws:iam::{ACCOUNT}:role/northwind-serving",
        runtime_role_arn=f"arn:aws:iam::{ACCOUNT}:role/NorthwindBedrockAgentCoreRuntime-us-east-1",
        registry_id="REG123456789",
        gateway_url="http://gateway.example",
        knowledge_bases={"alice": KB_ID, "live": "KBLIVE0000"},
        data_sources={"alice": DS_ID},
        images={"triage": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sklearn-inference:1.2-1"},
        direct_deploy=False,
    )


@pytest.fixture
def tenant() -> Tenant:
    return Tenant("alice", "northwind")


def described_package(arn=PKG_ARN, status="PendingManualApproval", metadata=None, version=3):
    return {
        "ModelPackageName": "northwind-alice-triage",
        "ModelPackageGroupName": "northwind-alice-triage",
        "ModelPackageVersion": version,
        "ModelPackageArn": arn,
        "CreationTime": 1700000000,
        "ModelPackageStatus": "Completed",
        "ModelPackageStatusDetails": {"ValidationStatuses": []},
        "ModelApprovalStatus": status,
        "InferenceSpecification": {
            "Containers": [
                {
                    "Image": "img",
                    "ModelDataUrl": "s3://northwind-artifacts/tenants/alice/triage/x/model.tar.gz",
                }
            ],
            "SupportedContentTypes": ["application/json"],
            "SupportedResponseMIMETypes": ["application/json"],
        },
        "CustomerMetadataProperties": metadata
        or {"metric:f1_macro": "0.91", "nw:tenant": "alice", "commit": "abc"},
    }


# ----- registry -----------------------------------------------------------------------------


def test_registry_register_uploads_and_creates_a_pending_package(cfg, tenant, tmp_path):
    sm, s3 = client("sagemaker"), client("s3")
    artifact = tmp_path / "triage"
    artifact.mkdir()
    (artifact / "model.joblib").write_bytes(b"model")
    (artifact / "MODEL_CARD.md").write_text("card")
    with Stubber(s3) as s3s, Stubber(sm) as sms:
        s3s.add_response(
            "put_object", {}, {"Bucket": "northwind-artifacts", "Key": ANY, "Body": ANY}
        )
        s3s.add_response(
            "put_object", {}, {"Bucket": "northwind-artifacts", "Key": ANY, "Body": ANY}
        )
        sms.add_response(
            "create_model_package",
            {"ModelPackageArn": PKG_ARN},
            {
                "ModelPackageGroupName": "northwind-alice-triage",
                "ModelPackageDescription": ANY,
                "InferenceSpecification": {
                    "Containers": [{"Image": cfg.images["triage"], "ModelDataUrl": ANY}],
                    "SupportedContentTypes": ["application/json"],
                    "SupportedResponseMIMETypes": ["application/json"],
                    "SupportedRealtimeInferenceInstanceTypes": ["ml.m5.large", "ml.m5.xlarge"],
                },
                "ModelApprovalStatus": "PendingManualApproval",
                "CustomerMetadataProperties": {
                    "commit": "abc",
                    "metric:f1_macro": "0.91",
                    "nw:tenant": "alice",
                    "nw:stage": "candidate",
                    # the deployer injects it as NW_MODEL_SHA256 and the handler checks it
                    "model_sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "ModelMetrics": ANY,
                "Tags": [
                    {"Key": "nw:tenant", "Value": "alice"},
                    {"Key": "nw:project", "Value": "triage"},
                ],
            },
        )
        registry = platform.SageMakerRegistry(sm, s3, cfg)
        assert isinstance(registry, ModelRegistry)
        v = registry.register(tenant, "triage", artifact, {"f1_macro": 0.91}, {"commit": "abc"})
    assert v.version == "3" and v.stage == Stage.CANDIDATE and v.tags["arn"] == PKG_ARN
    assert v.uri.startswith("s3://northwind-artifacts/tenants/alice/triage/")


def test_registry_tarball_puts_files_at_the_root(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("b")
    with tarfile.open(fileobj=io.BytesIO(platform._tarball(d)), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["a.txt", "sub/b.txt"]


def test_registry_register_needs_an_image(cfg, tenant, tmp_path):
    # the course projects fall back to the prebuilt images in nw.serving.sagemaker; any other
    # project must name its container, and the check happens before anything is uploaded
    registry = platform.SageMakerRegistry(client("sagemaker"), client("s3"), cfg)
    with pytest.raises(ValueError, match="NW_AWS_IMAGE_FORECAST"):
        registry.register(tenant, "forecast", tmp_path, {}, {})


def test_registry_register_falls_back_to_the_prebuilt_image():
    from nw.serving.sagemaker import IMAGES

    assert set(IMAGES) == {"triage", "semantic"}
    assert all(uri.startswith(("683313688378.", "763104351884.")) for uri in IMAGES.values())


def test_registry_set_stage_approves_and_live_promotes_into_the_live_group(cfg, tenant):
    sm = client("sagemaker")
    with Stubber(sm) as st:
        # the step-back rule reads who holds `live` first: nobody else here
        st.add_response(
            "list_model_packages",
            {"ModelPackageSummaryList": [summary(PKG_ARN, 3)]},
            listed("northwind-alice-triage"),
        )
        st.add_response(
            "describe_model_package", described_package(), {"ModelPackageName": PKG_ARN}
        )
        st.add_response(
            "describe_model_package", described_package(), {"ModelPackageName": PKG_ARN}
        )
        st.add_response(
            "update_model_package",
            {"ModelPackageArn": PKG_ARN},
            {
                "ModelPackageArn": PKG_ARN,
                "ModelApprovalStatus": "Approved",
                "ApprovalDescription": "gate passed",
                "CustomerMetadataProperties": {
                    "metric:f1_macro": "0.91",
                    "nw:tenant": "alice",
                    "commit": "abc",
                    "nw:stage": "live",
                },
            },
        )
        st.add_response(
            "create_model_package",
            {"ModelPackageArn": LIVE_PKG_ARN},
            {
                "ModelPackageGroupName": "northwind-live-triage",
                "ModelPackageDescription": ANY,
                "InferenceSpecification": described_package()["InferenceSpecification"],
                "ModelApprovalStatus": "Approved",
                "CustomerMetadataProperties": {
                    "metric:f1_macro": "0.91",
                    "nw:tenant": "alice",
                    "commit": "abc",
                    "nw:stage": "live",
                    "nw:source": PKG_ARN,
                    "nw:promoted_by": "alice",
                },
                "Tags": [
                    {"Key": "nw:tenant", "Value": "live"},
                    {"Key": "nw:project", "Value": "triage"},
                ],
            },
        )
        st.add_response(
            "describe_model_package",
            described_package(
                status="Approved", metadata={"metric:f1_macro": "0.91", "nw:stage": "live"}
            ),
            {"ModelPackageName": PKG_ARN},
        )
        registry = platform.SageMakerRegistry(sm, client("s3"), cfg)
        v = registry.set_stage(tenant, "triage", "3", Stage.LIVE, "gate passed")
    assert v.stage == Stage.LIVE and v.metrics == {"f1_macro": 0.91}


def summary(arn, version):
    return {
        "ModelPackageName": "n",
        "ModelPackageArn": arn,
        "ModelPackageVersion": version,
        "CreationTime": 1,
        "ModelPackageStatus": "Completed",
    }


def listed(group):
    return {"ModelPackageGroupName": group, "SortBy": "CreationTime", "SortOrder": "Ascending"}


def test_registry_versions_and_live_read_approval_status(cfg, tenant):
    """Oldest first; `Rejected` is retired; `live()` is the `nw:stage=live` package only,
    never an approved one (the contract's rules)."""
    sm = client("sagemaker")
    pkg2 = PKG_ARN[:-1] + "2"
    with Stubber(sm) as st:
        st.add_response(
            "list_model_packages",
            {"ModelPackageSummaryList": [summary(pkg2, 2), summary(PKG_ARN, 3)]},
            listed("northwind-alice-triage"),
        )
        st.add_response(
            "describe_model_package",
            described_package(arn=pkg2, status="Approved", version=2),
            {"ModelPackageName": pkg2},
        )
        st.add_response(
            "describe_model_package",
            described_package(status="Rejected"),
            {"ModelPackageName": PKG_ARN},
        )
        registry = platform.SageMakerRegistry(sm, client("s3"), cfg)
        versions = registry.versions(tenant, "triage")
        assert [v.version for v in versions] == ["2", "3"]
        assert [v.stage for v in versions] == [Stage.APPROVED, Stage.RETIRED]
    with Stubber(sm) as st:
        st.add_response("list_model_packages", {"ModelPackageSummaryList": [summary(PKG_ARN, 3)]})
        st.add_response("describe_model_package", described_package(status="Approved"))
        assert registry.live(tenant, "triage") is None, "approved is not live"
    with Stubber(sm) as st:
        st.add_response("list_model_packages", {"ModelPackageSummaryList": [summary(PKG_ARN, 3)]})
        st.add_response(
            "describe_model_package",
            described_package(status="Approved", metadata={"nw:stage": "live"}),
        )
        live = registry.live(tenant, "triage")
    assert live is not None and live.version == "3" and live.stage == Stage.LIVE


def test_registry_of_a_group_that_does_not_exist_is_empty(cfg, tenant):
    sm = client("sagemaker")
    with Stubber(sm) as st:
        st.add_client_error("list_model_packages", service_error_code="ValidationException")
        assert platform.SageMakerRegistry(sm, client("s3"), cfg).versions(tenant, "triage") == []


def test_one_registration_path_the_pipeline_step_uses_the_registry(cfg):
    """The SageMaker pipeline's Register step runs `nw.pipelines.steps.register` under this
    registry, so a pipeline package and a bootstrap package carry the same metadata."""
    pytest.importorskip("sagemaker.mlops.workflow.pipeline")
    from nw.pipelines.sagemaker import SageMakerConfig, fake_session
    from nw.pipelines.sagemaker.definitions import definition

    config = SageMakerConfig(
        tenant=Tenant("alice"), role_arn="arn:aws:iam::1:role/r", image_uri="img", bucket="b"
    )
    d = definition("triage", config, fake_session())
    [register] = d["Steps"][-1]["Arguments"]["IfSteps"]
    assert register["Type"] == "Processing", "no RegisterModel step with its own metadata"
    assert register["Arguments"]["AppSpecification"]["ContainerEntrypoint"][-1] == (
        "nw.pipelines.steps.register"
    )


def test_a_packaging_error_is_not_swallowed(tmp_path, monkeypatch):
    """Only the kind check may say 'not servable'; a missing file while packaging a triage
    artifact propagates instead of shipping a tarball without inference.py."""
    from nw.serving.sagemaker import package as pkg

    d = tmp_path / "m"
    d.mkdir()
    (d / "model.joblib").write_bytes(b"m")

    def broken(*a, **k):
        raise FileNotFoundError("inference.py source missing")

    monkeypatch.setattr(pkg, "package", broken)
    with pytest.raises(FileNotFoundError, match="inference.py"):
        platform._tarball(d)


def test_registry_download_extracts_the_tarball(cfg, tenant, tmp_path):
    s3 = client("s3")
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.joblib").write_bytes(b"m")
    body = platform._tarball(src)
    with Stubber(s3) as st:
        st.add_response(
            "get_object",
            {"Body": io.BytesIO(body)},
            {"Bucket": "northwind-artifacts", "Key": "tenants/alice/triage/x/model.tar.gz"},
        )
        registry = platform.SageMakerRegistry(client("sagemaker"), s3, cfg)
        out = registry.download(
            tenant,
            ModelVersion(
                "triage",
                "3",
                Stage.APPROVED,
                "s3://northwind-artifacts/tenants/alice/triage/x/model.tar.gz",
            ),
            tmp_path / "out",
        )
    assert (out / "model.joblib").read_bytes() == b"m"


# ----- pipelines ----------------------------------------------------------------------------


def test_pipelines_submit_status_wait_and_logs(cfg, tenant):
    sm, logs = client("sagemaker"), client("logs")
    naps: list[float] = []
    steps = {
        "PipelineExecutionSteps": [
            {
                "StepName": "train",
                "StepStatus": "Succeeded",
                "Metadata": {
                    "TrainingJob": {
                        "Arn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:training-job/pipelines-abc-train-xyz"
                    }
                },
            },
            {
                "StepName": "register",
                "StepStatus": "Succeeded",
                "Metadata": {"RegisterModel": {"Arn": PKG_ARN}},
            },
        ]
    }
    with Stubber(sm) as st, Stubber(logs) as lg:
        st.add_response(
            "describe_pipeline",
            {"PipelineArn": PIPELINE_ARN, "PipelineName": "northwind-alice-triage"},
            {"PipelineName": "northwind-alice-triage"},
        )
        st.add_response(
            "update_pipeline", {"PipelineArn": PIPELINE_ARN}, upserted("northwind-alice-triage")
        )
        st.add_response(
            "start_pipeline_execution",
            {"PipelineExecutionArn": EXEC_ARN},
            {
                "PipelineName": "northwind-alice-triage",
                "PipelineExecutionDisplayName": ANY,
                "PipelineParameters": [
                    {"Name": "Epochs", "Value": "3"},
                    {"Name": "SourceUri", "Value": SOURCE_URI},
                    {"Name": "DataUri", "Value": "s3://northwind-data/data/tickets/"},
                    {
                        "Name": "OutputRoot",
                        "Value": "s3://northwind-artifacts/tenants/alice/pipelines",
                    },
                    {
                        "Name": "ProductionSummary",
                        "Value": "s3://northwind-artifacts/baselines/triage_production.json",
                    },
                ],
                "ClientRequestToken": ANY,
            },
        )
        st.add_response(
            "describe_pipeline_execution",
            {"PipelineExecutionStatus": "Executing"},
            {"PipelineExecutionArn": EXEC_ARN},
        )
        st.add_response(
            "describe_pipeline_execution",
            {"PipelineExecutionStatus": "Succeeded"},
            {"PipelineExecutionArn": EXEC_ARN},
        )
        st.add_response(
            "list_pipeline_execution_steps",
            steps,
            {"PipelineExecutionArn": EXEC_ARN, "SortOrder": "Ascending"},
        )
        st.add_response(
            "list_pipeline_execution_steps",
            steps,
            {"PipelineExecutionArn": EXEC_ARN, "SortOrder": "Ascending"},
        )
        lg.add_response(
            "filter_log_events",
            {"events": [{"message": "epoch 1 loss 0.4"}, {"message": "epoch 2 loss 0.3"}]},
            {
                "logGroupName": "/aws/sagemaker/TrainingJobs",
                "logStreamNamePrefix": "pipelines-abc-train-xyz",
                "limit": 1000,
            },
        )
        s3 = source_s3()
        runner = platform.SageMakerPipelines(
            sm,
            logs,
            pipeline_cfg(cfg),
            sleep=naps.append,
            poll_s=5,
            definitions=fake_definition,
            s3=s3,
            bundler=lambda: BUNDLE,
        )
        assert isinstance(runner, PipelineRunner)
        run = runner.submit(tenant, "triage", {"Epochs": 3})
        # the learner's code was uploaded once and handed to the run
        assert s3.objects[("northwind-artifacts", SOURCE_KEY)] == b"bundle"
        assert (
            run.status == RunStatus.QUEUED
            and run.run_id == EXEC_ARN
            and "console.aws.amazon.com" in (run.url or "")
        )
        done = runner.wait(tenant, run, timeout_s=60)
        assert done.status == RunStatus.SUCCEEDED and done.outputs["register"] == PKG_ARN
        assert naps == [5]
        lines = list(runner.logs(tenant, run))
    assert lines[0].startswith("[Succeeded] train") and "epoch 2 loss 0.3" in lines[2]


PIPELINE_ARN = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:pipeline/northwind-alice-triage"
PIPELINE_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/nw-pipelines:test"
DEFINITION = '{"Version": "2020-12-01", "Steps": []}'
SOURCE_KEY = "tenants/alice/source/nw-source-0123456789ab.tar.gz"
SOURCE_URI = f"s3://northwind-artifacts/{SOURCE_KEY}"


def _bundle():
    import tempfile
    from pathlib import Path

    from nw.pipelines.source import Bundle

    path = Path(tempfile.mkdtemp()) / "nw-source-0123456789ab.tar.gz"
    path.write_bytes(b"bundle")
    return Bundle(path=path, sha256_12="0123456789ab", git_sha="abc", files=1)


BUNDLE = _bundle()


def source_s3():
    from tests.platform.aws_fakes import FakeS3

    return FakeS3()


def pipeline_cfg(cfg):
    from dataclasses import replace

    return replace(cfg, pipeline_image=PIPELINE_IMAGE)


def fake_definition(pipeline, config):
    """Stands in for `nw.pipelines.sagemaker.definition`, which needs the SDK; records what
    the runner asked for."""
    fake_definition.calls.append((pipeline, config))
    return DEFINITION


fake_definition.calls = []


def upserted(name):
    return {
        "PipelineName": name,
        "PipelineDefinition": DEFINITION,
        "PipelineDescription": ANY,
        "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/northwind-alice-sagemaker",
    }


def test_submit_on_a_missing_pipeline_creates_it_then_starts_it(cfg, tenant):
    sm = client("sagemaker")
    fake_definition.calls.clear()
    with Stubber(sm) as st:
        st.add_client_error(
            "describe_pipeline",
            service_error_code="ResourceNotFound",
            http_status_code=400,
            expected_params={"PipelineName": "northwind-alice-triage"},
        )
        st.add_response(
            "create_pipeline",
            {"PipelineArn": PIPELINE_ARN},
            {
                **upserted("northwind-alice-triage"),
                "PipelineDisplayName": "northwind-alice-triage",
                "ClientRequestToken": ANY,
                "Tags": [
                    {"Key": "tenant", "Value": "alice"},
                    {"Key": "environment", "Value": "northwind"},
                ],
            },
        )
        st.add_response(
            "start_pipeline_execution",
            {"PipelineExecutionArn": EXEC_ARN},
            {
                "PipelineName": "northwind-alice-triage",
                "PipelineExecutionDisplayName": ANY,
                "PipelineParameters": [
                    {"Name": "Force", "Value": "true"},
                    {"Name": "MinP0Recall", "Value": "0.9"},
                    {"Name": "SourceUri", "Value": SOURCE_URI},
                    {"Name": "DataUri", "Value": "s3://northwind-data/data/tickets/"},
                    {
                        "Name": "OutputRoot",
                        "Value": "s3://northwind-artifacts/tenants/alice/pipelines",
                    },
                    {
                        "Name": "ProductionSummary",
                        "Value": "s3://northwind-artifacts/baselines/triage_production.json",
                    },
                ],
                "ClientRequestToken": ANY,
            },
        )
        runner = platform.SageMakerPipelines(
            sm,
            client("logs"),
            pipeline_cfg(cfg),
            definitions=fake_definition,
            s3=source_s3(),
            bundler=lambda: BUNDLE,
        )
        run = runner.submit(
            tenant,
            "retrain-triage",
            {"tenant": "alice", "environment": "northwind", "force": True, "min_p0_recall": 0.9},
        )
        st.assert_no_pending_responses()
    assert run.run_id == EXEC_ARN and run.pipeline == "retrain-triage"
    ((kind, config),) = fake_definition.calls
    assert kind == "triage" and config.image_uri == PIPELINE_IMAGE
    assert config.pipeline_name("triage") == "northwind-alice-triage"
    assert config.bucket == "northwind-artifacts" and config.region == REGION
    # The definition's defaults are the deployed locations, so a scheduled run that passes
    # nothing still reads the S3 tickets and the production summary the deploy uploaded.
    # The definition's `SourceUri` defaults to the bundle, so the weekly schedule runs the last
    # submitted code, not the image's.
    assert config.defaults == {
        "data_uri": "s3://northwind-data/data/tickets/",
        "output_root": "s3://northwind-artifacts/tenants/alice/pipelines",
        "production_summary": "s3://northwind-artifacts/baselines/triage_production.json",
        "source_uri": SOURCE_URI,
    }


def test_upsert_on_an_existing_pipeline_updates_it_and_is_idempotent(cfg, tenant):
    sm = client("sagemaker")
    role = f"arn:aws:iam::{ACCOUNT}:role/custom-pipelines"
    with Stubber(sm) as st:
        for _ in range(2):
            st.add_response(
                "describe_pipeline",
                {"PipelineArn": PIPELINE_ARN},
                {"PipelineName": "northwind-alice-semantic"},
            )
            st.add_response(
                "update_pipeline",
                {"PipelineArn": PIPELINE_ARN},
                {**upserted("northwind-alice-semantic"), "RoleArn": role},
            )
        s3 = source_s3()
        runner = platform.SageMakerPipelines(
            sm,
            client("logs"),
            pipeline_cfg(cfg),
            definitions=fake_definition,
            s3=s3,
            bundler=lambda: BUNDLE,
        )
        assert runner.upsert(tenant, "semantic", {"role_arn": role}) == PIPELINE_ARN
        assert runner.upsert(tenant, "semantic", {"role_arn": role}) == PIPELINE_ARN
        st.assert_no_pending_responses()
    assert list(s3.objects) == [("northwind-artifacts", SOURCE_KEY)], "uploaded once"


def test_upsert_without_s3_refuses_to_run_the_image_code(cfg, tenant):
    runner = platform.SageMakerPipelines(
        client("sagemaker"), client("logs"), pipeline_cfg(cfg), definitions=fake_definition
    )
    with pytest.raises(ValueError, match="image's code"):
        runner.upsert(tenant, "triage")


def test_upsert_needs_the_pipelines_image(cfg, tenant):
    runner = platform.SageMakerPipelines(
        client("sagemaker"), client("logs"), cfg, definitions=fake_definition
    )
    with pytest.raises(ValueError, match="NW_AWS_PIPELINE_IMAGE"):
        runner.upsert(tenant, "triage")
    with pytest.raises(ValueError, match="unknown pipeline"):
        runner.upsert(tenant, "policy", {"image_uri": PIPELINE_IMAGE})


def test_pipelines_wait_times_out(cfg, tenant):
    sm = client("sagemaker")
    with Stubber(sm) as st:
        for _ in range(3):
            st.add_response("describe_pipeline_execution", {"PipelineExecutionStatus": "Executing"})
        runner = platform.SageMakerPipelines(
            sm, client("logs"), cfg, sleep=lambda s: None, poll_s=0
        )
        with pytest.raises(TimeoutError):
            runner.wait(tenant, PipelineRun("triage", EXEC_ARN, RunStatus.RUNNING), timeout_s=0)


# ----- endpoints ----------------------------------------------------------------------------


def test_endpoints_deploy_in_cohort_mode_is_the_approval(cfg, tenant):
    sm = client("sagemaker")
    with Stubber(sm) as st:
        st.add_response(
            "list_model_packages",
            {"ModelPackageSummaryList": [summary(PKG_ARN, 3)]},
            listed("northwind-alice-triage"),
        )
        st.add_response(
            "describe_model_package", described_package(), {"ModelPackageName": PKG_ARN}
        )
        st.add_response(
            "describe_model_package", described_package(), {"ModelPackageName": PKG_ARN}
        )
        st.add_response(
            "update_model_package",
            {"ModelPackageArn": PKG_ARN},
            {
                "ModelPackageArn": PKG_ARN,
                "ModelApprovalStatus": "Approved",
                "ApprovalDescription": ANY,
                "CustomerMetadataProperties": ANY,
            },
        )
        st.add_response(
            "describe_model_package",
            described_package(status="Approved"),
            {"ModelPackageName": PKG_ARN},
        )
        registry = platform.SageMakerRegistry(sm, client("s3"), cfg)
        endpoints = platform.SageMakerEndpoints(sm, client("sagemaker-runtime"), registry, cfg)
        assert isinstance(endpoints, EndpointClient)
        name = endpoints.deploy(
            tenant, ModelVersion("triage", "3", Stage.CANDIDATE, "s3://x", tags={"arn": PKG_ARN})
        )
    assert name == "northwind-alice-triage"


def test_endpoints_deploy_in_solo_mode_creates_a_serverless_endpoint(cfg, tenant):
    cfg.direct_deploy = True
    sm = client("sagemaker")
    with Stubber(sm) as st:
        st.add_response(
            "create_model",
            {"ModelArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:model/northwind-alice-triage-1"},
            {
                "ModelName": ANY,
                "PrimaryContainer": {"ModelPackageName": PKG_ARN},
                "ExecutionRoleArn": cfg.serving_role_arn,
            },
        )
        st.add_response(
            "create_endpoint_config",
            {
                "EndpointConfigArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint-config/northwind-alice-triage-1"
            },
            {
                "EndpointConfigName": ANY,
                "ProductionVariants": [
                    {
                        "VariantName": "AllTraffic",
                        "ModelName": ANY,
                        "ServerlessConfig": {"MemorySizeInMB": 3072, "MaxConcurrency": 5},
                    }
                ],
            },
        )
        st.add_client_error(
            "describe_endpoint",
            "ValidationException",
            "Could not find endpoint",
            expected_params={"EndpointName": "northwind-alice-triage"},
        )
        st.add_response(
            "create_endpoint",
            {
                "EndpointArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/northwind-alice-triage"
            },
            {"EndpointName": "northwind-alice-triage", "EndpointConfigName": ANY},
        )
        registry = platform.SageMakerRegistry(sm, client("s3"), cfg)
        endpoints = platform.SageMakerEndpoints(sm, client("sagemaker-runtime"), registry, cfg)
        name = endpoints.deploy(
            tenant, ModelVersion("triage", "3", Stage.APPROVED, "s3://x", tags={"arn": PKG_ARN})
        )
    assert name == "northwind-alice-triage"


def test_endpoints_invoke_status_and_delete(cfg, tenant):
    sm, rt = client("sagemaker"), client("sagemaker-runtime")
    with Stubber(sm) as st, Stubber(rt) as rs:
        rs.add_response(
            "invoke_endpoint",
            {
                "Body": io.BytesIO(json.dumps({"priority": "high", "confidence": 0.9}).encode()),
                "ContentType": "application/json",
            },
            {
                "EndpointName": "northwind-alice-triage",
                "ContentType": "application/json",
                "Accept": "application/json",
                "Body": json.dumps({"subject": "s", "body": "b"}).encode(),
            },
        )
        st.add_response(
            "describe_endpoint",
            {
                "EndpointName": "northwind-alice-triage",
                "EndpointArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/northwind-alice-triage",
                "EndpointConfigName": "cfg-1",
                "EndpointStatus": "InService",
                "CreationTime": 1,
                "LastModifiedTime": 1,
                "ProductionVariants": [{"VariantName": "AllTraffic"}],
            },
            {"EndpointName": "northwind-alice-triage"},
        )
        st.add_response(
            "describe_endpoint",
            {
                "EndpointName": "northwind-alice-triage",
                "EndpointArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:endpoint/northwind-alice-triage",
                "EndpointConfigName": "cfg-1",
                "EndpointStatus": "InService",
                "CreationTime": 1,
                "LastModifiedTime": 1,
            },
        )
        st.add_response("delete_endpoint", {}, {"EndpointName": "northwind-alice-triage"})
        st.add_response("delete_endpoint_config", {}, {"EndpointConfigName": "cfg-1"})
        st.add_client_error("describe_endpoint", "ValidationException", "Could not find endpoint")
        registry = platform.SageMakerRegistry(sm, client("s3"), cfg)
        endpoints = platform.SageMakerEndpoints(sm, rt, registry, cfg)
        assert (
            endpoints.invoke(tenant, "triage", {"subject": "s", "body": "b"})["priority"] == "high"
        )
        assert endpoints.status(tenant, "triage") == {
            "status": "InService",
            "endpoint": "northwind-alice-triage",
            "config": "cfg-1",
            "variants": ["AllTraffic"],
            "capture": False,
            "failure": None,
        }
        endpoints.delete(tenant, "triage")
        assert endpoints.status(tenant, "triage")["status"] == "absent"


# ----- prompts ------------------------------------------------------------------------------


def prompt_body(version: str, text: str = "Answer from policy.") -> dict:
    return {
        "name": "northwind-alice-policy-answer",
        "id": "PROMPT1234",
        "arn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:prompt/PROMPT1234"
        + (f":{version}" if version != "DRAFT" else ""),
        "version": version,
        "createdAt": "2026-09-29T00:00:00Z",
        "updatedAt": "2026-09-29T00:00:00Z",
        "variants": [
            {
                "name": "default",
                "templateType": "TEXT",
                "templateConfiguration": {"text": {"text": text}},
            }
        ],
    }


def test_prompts_register_creates_a_prompt_and_a_version_with_the_hash(cfg, tenant):
    agent = client("bedrock-agent")
    text = "Answer from policy."
    sha = platform.prompt_hash(text)
    with Stubber(agent) as st:
        st.add_response("list_prompts", {"promptSummaries": []}, {"maxResults": 100})
        st.add_response(
            "create_prompt",
            prompt_body("DRAFT", text),
            {
                "name": "northwind-alice-policy-answer",
                "description": f"policy.answer@{sha}",
                "defaultVariant": "default",
                "variants": [
                    {
                        "name": "default",
                        "templateType": "TEXT",
                        "templateConfiguration": {"text": {"text": text}},
                    }
                ],
                "tags": {
                    "owner": "alice",
                    "nw:prompt": "policy.answer",
                    "nw:sha256_12": sha,
                    "nw:tenant": "alice",
                },
            },
        )
        st.add_response(
            "create_prompt_version",
            prompt_body("1", text),
            {"promptIdentifier": "PROMPT1234", "description": sha, "tags": ANY},
        )
        st.add_response(
            "tag_resource",
            {},
            {
                "resourceArn": prompt_body("1")["arn"],
                "tags": {
                    "owner": "alice",
                    "nw:prompt": "policy.answer",
                    "nw:sha256_12": sha,
                    "nw:tenant": "alice",
                    "nw:stage": "candidate",
                },
            },
        )
        store = platform.BedrockPrompts(agent, cfg)
        assert isinstance(store, PromptStore)
        v = store.register(tenant, "policy.answer", text, {"owner": "alice"})
    assert v.version == "1" and v.sha256_12 == sha and v.stage == Stage.CANDIDATE


def test_prompts_get_set_stage_and_versions(cfg, tenant):
    """On a stateful Bedrock fake: the live version wins `get`, a stage moves with the previous
    holder stepping back, the same text registers once, versions are oldest first."""
    from tests.platform.aws_fakes import FakeBedrockAgent

    agent = FakeBedrockAgent()
    store = platform.BedrockPrompts(agent, cfg)
    text = "Answer from policy."
    a = store.register(tenant, "policy.answer", text, {"owner": "alice"})
    assert store.register(tenant, "policy.answer", text, {"owner": "alice"}).version == a.version
    assert agent.created_versions == 1, "no second Bedrock version for the same text"
    b = store.register(tenant, "policy.answer", "Answer from policy, cite it.", {})
    assert [v.version for v in store.versions(tenant, "policy.answer")] == ["1", "2"]
    store.set_stage(tenant, "policy.answer", "1", Stage.LIVE)
    got = store.get(tenant, "policy.answer")
    assert (got.version, got.stage, got.text, got.tags) == (
        "1",
        Stage.LIVE,
        text,
        {"owner": "alice"},
    )
    arn = f"arn:aws:bedrock:us-east-1:123456789012:prompt/{agent.list_prompts()['promptSummaries'][0]['id']}"
    assert agent.tags[arn]["nw:live_version"] == "1"
    store.set_stage(tenant, "policy.answer", b.version, Stage.LIVE)
    stages = {v.version: v.stage for v in store.versions(tenant, "policy.answer")}
    assert stages == {"1": Stage.RETIRED, "2": Stage.LIVE}


def test_prompts_get_unknown_raises(cfg, tenant):
    agent = client("bedrock-agent")
    with Stubber(agent) as st:
        st.add_response("list_prompts", {"promptSummaries": []}, {"maxResults": 100})
        with pytest.raises(KeyError):
            platform.BedrockPrompts(agent, cfg).get(tenant, "policy.answer")


# ----- vectors ------------------------------------------------------------------------------


def test_vectors_upsert_writes_documents_and_syncs(cfg, tenant):
    agent, rt, s3 = client("bedrock-agent"), client("bedrock-agent-runtime"), client("s3")
    naps: list[float] = []
    job = {
        "knowledgeBaseId": KB_ID,
        "dataSourceId": DS_ID,
        "ingestionJobId": "JOB1",
        "status": "IN_PROGRESS",
        "startedAt": "2026-09-29T00:00:00Z",
        "updatedAt": "2026-09-29T00:00:00Z",
    }
    with Stubber(agent) as st, Stubber(s3) as s3s:
        s3s.add_response(
            "put_object",
            {},
            {
                "Bucket": "northwind-data",
                "Key": "tenants/alice/policies/refunds-1.txt",
                "Body": b"Refunds within 30 days.",
            },
        )
        s3s.add_response(
            "put_object",
            {},
            {
                "Bucket": "northwind-data",
                "Key": "tenants/alice/policies/refunds-1.txt.metadata.json",
                "Body": json.dumps(
                    {"metadataAttributes": {"chunk_id": "refunds-1", "doc": "refunds"}}
                ).encode(),
            },
        )
        st.add_response(
            "start_ingestion_job",
            {"ingestionJob": job},
            {"knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "clientToken": ANY},
        )
        st.add_response(
            "get_ingestion_job",
            {"ingestionJob": {**job, "status": "COMPLETE"}},
            {"knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "ingestionJobId": "JOB1"},
        )
        store = platform.BedrockKnowledgeBase(agent, rt, s3, cfg, sleep=naps.append, poll_s=1)
        assert isinstance(store, VectorStore)
        n = store.upsert(
            tenant,
            "policies",
            ["refunds-1"],
            ["Refunds within 30 days."],
            None,
            [{"doc": "refunds"}],
        )
    assert n == 1 and naps == [1]
    with pytest.raises(ValueError):
        store.upsert(tenant, "tickets", [], [], None, [])


def test_vectors_search_count_and_drop(cfg, tenant):
    agent, rt, s3 = client("bedrock-agent"), client("bedrock-agent-runtime"), client("s3")
    with Stubber(agent) as st, Stubber(rt) as rs, Stubber(s3) as s3s:
        rs.add_response(
            "retrieve",
            {
                "retrievalResults": [
                    {
                        "content": {"text": "Refunds within 30 days."},
                        "score": 0.82,
                        "metadata": {"chunk_id": "refunds-1", "doc": "refunds"},
                        "location": {
                            "type": "S3",
                            "s3Location": {
                                "uri": "s3://northwind-data/tenants/alice/policies/refunds-1.txt"
                            },
                        },
                    },
                    {
                        "content": {"text": "Escalate after 2 days."},
                        "score": 0.5,
                        "location": {
                            "type": "S3",
                            "s3Location": {
                                "uri": "s3://northwind-data/tenants/alice/policies/sla-2.txt"
                            },
                        },
                    },
                ]
            },
            {
                "knowledgeBaseId": KB_ID,
                "retrievalQuery": {"text": "refund window"},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 2}},
            },
        )
        s3s.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "tenants/alice/policies/refunds-1.txt"},
                    {"Key": "tenants/alice/policies/refunds-1.txt.metadata.json"},
                ]
            },
            {"Bucket": "northwind-data", "Prefix": "tenants/alice/policies/"},
        )
        s3s.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "tenants/alice/policies/refunds-1.txt"}]},
            {"Bucket": "northwind-data", "Prefix": "tenants/alice/policies/"},
        )
        s3s.add_response(
            "delete_objects",
            {},
            {
                "Bucket": "northwind-data",
                "Delete": {
                    "Objects": [{"Key": "tenants/alice/policies/refunds-1.txt"}],
                    "Quiet": True,
                },
            },
        )
        job = {
            "knowledgeBaseId": KB_ID,
            "dataSourceId": DS_ID,
            "ingestionJobId": "JOB2",
            "status": "COMPLETE",
            "startedAt": "2026-09-29T00:00:00Z",
            "updatedAt": "2026-09-29T00:00:00Z",
        }
        st.add_response("start_ingestion_job", {"ingestionJob": job})
        store = platform.BedrockKnowledgeBase(agent, rt, s3, cfg, sleep=lambda s: None)
        hits = store.search(tenant, "policies", "refund window", k=2)
        assert [h.id for h in hits] == ["refunds-1", "sla-2"] and hits[0].score == 0.82
        assert hits[0].metadata["doc"] == "refunds" and hits[1].metadata["source"].endswith(
            "sla-2.txt"
        )
        assert store.count(tenant, "policies") == 1
        store.drop(tenant, "policies")


# ----- agents --------------------------------------------------------------------------------


def runtime_summary(name="northwind_alice_resolver"):
    return {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": "northwind_alice_resolver-abc",
        "agentRuntimeName": name,
        "agentRuntimeVersion": "1",
        "description": "d",
        "lastUpdatedAt": "2026-09-29T00:00:00Z",
        "status": "READY",
    }


def test_agents_deploy_creates_then_updates(cfg, tenant):
    control = client("bedrock-agentcore-control")
    created = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": "northwind_alice_resolver-abc",
        "agentRuntimeVersion": "1",
        "createdAt": "2026-09-29T00:00:00Z",
        "status": "CREATING",
    }
    with Stubber(control) as st:
        st.add_response("list_agent_runtimes", {"agentRuntimes": []}, {"maxResults": 100})
        st.add_response(
            "create_agent_runtime",
            created,
            {
                "agentRuntimeName": "northwind_alice_resolver",
                "agentRuntimeArtifact": {
                    "containerConfiguration": {"containerUri": "img@sha256:abc"}
                },
                "roleArn": cfg.runtime_role_arn,
                "networkConfiguration": {"networkMode": "PUBLIC"},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "environmentVariables": {
                    "NW_SPEND_CAP_USD": "10",
                    "NW_TRACK": "aws",
                    "NW_ENVIRONMENT": "northwind",
                    "NW_TENANT": "alice",
                    "NW_AGENT_VERSION": "v7",
                    "NW_APP": "nw.agent.agentcore:app",
                    "NW_AGENT_ROLE": "resolver",
                    "PORT": "8080",
                    "NW_RUNTIME_AUTH": "platform",
                    "NW_REDACT_DETECTOR": "heuristic",
                },
                "description": "alice resolver, agent version v7",
                "clientToken": ANY,
            },
        )
        st.add_response(
            "update_agent_runtime",
            {**created, "status": "UPDATING", "lastUpdatedAt": "2026-09-29T00:00:00Z"},
            {
                "agentRuntimeId": "northwind_alice_resolver-abc",
                "agentRuntimeArtifact": {
                    "containerConfiguration": {"containerUri": "img@sha256:def"}
                },
                "roleArn": cfg.runtime_role_arn,
                "networkConfiguration": {"networkMode": "PUBLIC"},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "environmentVariables": ANY,
                "description": ANY,
            },
        )
        agents = platform.AgentCoreRuntime(
            control, client("bedrock-agentcore"), client("agent-registry-control"), cfg
        )
        assert isinstance(agents, AgentRuntime)
        assert (
            agents.deploy(tenant, "img@sha256:abc", {"NW_SPEND_CAP_USD": "10"}, version="v7")
            == RUNTIME_ARN
        )
        assert agents.deploy(tenant, "img@sha256:def", {}, version="v8") == RUNTIME_ARN


def test_agents_invoke_uses_a_long_session_id_and_parses_json(cfg, tenant):
    control, data = client("bedrock-agentcore-control"), client("bedrock-agentcore")
    with Stubber(control) as st, Stubber(data) as ds:
        st.add_response(
            "list_agent_runtimes", {"agentRuntimes": [runtime_summary()]}, {"maxResults": 100}
        )
        ds.add_response(
            "invoke_agent_runtime",
            {
                "contentType": "application/json",
                "response": io.BytesIO(b'{"answer": "Refund approved", "cost_usd": 0.01}'),
                "runtimeSessionId": "alice-session-000000000000000000000000000000",
            },
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "runtimeSessionId": ANY,
                "contentType": "application/json",
                "accept": "application/json",
                "payload": json.dumps({"task": "refund?"}).encode(),
            },
        )
        agents = platform.AgentCoreRuntime(control, data, client("agent-registry-control"), cfg)
        out = agents.invoke(tenant, {"task": "refund?"}, session_id="short")
    assert out["answer"] == "Refund approved" and len(out["session_id"]) >= 33


def test_agents_register_submits_the_card_and_handles_conflicts(cfg, tenant):
    reg = client("agent-registry-control")
    card = {
        "name": "alice resolver",
        "description": "resolves tickets",
        "version": "1.2.0",
        "url": "https://x",
    }
    with Stubber(reg) as st:
        st.add_response(
            "create_registry_record",
            {"recordArn": RECORD_ARN, "status": "CREATING"},
            {
                "registryId": "REG123456789",
                "name": "northwind-alice-resolver",
                "displayName": "alice resolver",
                "description": "resolves tickets",
                "recordType": "AGENT",
                "descriptors": {
                    "a2aAgentCard": {"data": json.dumps(card), "dataSchemaVersion": "0.3"}
                },
                "recordVersion": "1.2.0",
                "clientToken": ANY,
            },
        )
        st.add_response(
            "submit_registry_record_for_approval",
            SUBMITTED,
            {"registryId": "REG123456789", "recordId": "REC1234567890"},
        )
        agents = platform.AgentCoreRuntime(
            client("bedrock-agentcore-control"), client("bedrock-agentcore"), reg, cfg
        )
        assert agents.register(tenant, card) == RECORD_ARN
    with Stubber(reg) as st:
        st.add_client_error("create_registry_record", "ConflictException", "exists")
        st.add_response(
            "list_registry_records",
            {"registryRecords": [RECORD_SUMMARY]},
            {"registryId": "REG123456789", "maxResults": 100},
        )
        st.add_response(
            "update_registry_record",
            {**RECORD_SUMMARY, "status": "UPDATING"},
            {
                "registryId": "REG123456789",
                "recordId": "REC1234567890",
                "descriptors": ANY,
                "recordVersion": "1.2.0",
            },
        )
        st.add_response("submit_registry_record_for_approval", SUBMITTED)
        assert agents.register(tenant, card) == RECORD_ARN


def test_agents_status(cfg, tenant):
    control = client("bedrock-agentcore-control")
    with Stubber(control) as st:
        st.add_response(
            "list_agent_runtimes", {"agentRuntimes": [runtime_summary()]}, {"maxResults": 100}
        )
        st.add_response(
            "get_agent_runtime",
            {
                **runtime_summary(),
                "createdAt": "2026-09-29T00:00:00Z",
                "roleArn": cfg.runtime_role_arn,
                "lifecycleConfiguration": {},
                "environmentVariables": {"NW_AGENT_VERSION": "v7"},
            },
            {"agentRuntimeId": "northwind_alice_resolver-abc"},
        )
        agents = platform.AgentCoreRuntime(
            control, client("bedrock-agentcore"), client("agent-registry-control"), cfg
        )
        s = agents.status(tenant)
    assert s["status"] == "READY" and s["agent_version"] == "v7" and s["arn"] == RUNTIME_ARN
    with Stubber(control) as st:
        st.add_response("list_agent_runtimes", {"agentRuntimes": []}, {"maxResults": 100})
        assert agents.status(Tenant("bob", "northwind"))["status"] == "absent"


# ----- wiring and configuration ------------------------------------------------------------


def test_build_wires_every_protocol(cfg, monkeypatch):
    from nw.config import Settings, Track

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    p = platform.build(Settings(track=Track.AWS, _env_file=None), cfg)
    assert p.track == Track.AWS and p.gateway_url == "http://gateway.example"
    assert (
        p.describe()["registry"] == "SageMakerRegistry"
        and p.describe()["agents"] == "AgentCoreRuntime"
    )
    for proto, impl in (
        (ModelRegistry, p.registry),
        (PipelineRunner, p.pipelines),
        (EndpointClient, p.endpoints),
        (PromptStore, p.prompts),
        (VectorStore, p.vectors),
        (AgentRuntime, p.agents),
    ):
        assert isinstance(impl, proto)


def test_config_reads_outputs_json_and_environment(tmp_path, monkeypatch):
    from nw.config import Settings, Track

    out = tmp_path / "outputs.json"
    out.write_text(
        json.dumps(
            {
                "northwind-platform": {
                    "Environment": "northwind",
                    "DataBucket": "d",
                    "ArtifactsBucket": "a",
                    "ServingRoleArn": "arn:s",
                    "RuntimeRoleArn": "arn:r",
                    "RegistryId": "REG1",
                    "GatewayUrl": "http://g",
                    "KnowledgeBases": "alice=KB1,live=KB2",
                    "PipelineImage": "/northwind/images/pipelines",
                    "ServingRoleArns": "alice=arn:serving-alice,bob=arn:serving-bob",
                    "RuntimeRoleArns": "alice=arn:agentcore-alice",
                    "TenantApiKeys": "alice=arn:secret-alice",
                    "AgentNetworkMode": "VPC",
                    "AgentSubnets": "subnet-1,subnet-2",
                    "AgentSecurityGroup": "sg-1",
                }
            }
        )
    )
    monkeypatch.delenv("NW_AWS_DIRECT_DEPLOY", raising=False)
    monkeypatch.setenv("NW_AWS_IMAGE_TRIAGE", "img:1")
    cfg = platform.AwsPlatformConfig.from_settings(Settings(track=Track.AWS, _env_file=None), out)
    assert (
        cfg.data_bucket == "d"
        and cfg.knowledge_bases == {"alice": "KB1", "live": "KB2"}
        and cfg.images == {"triage": "img:1"}
    )
    assert cfg.direct_deploy is True, "no tenant set means solo, which deploys directly"
    assert cfg.pipeline_image == "/northwind/images/pipelines", "an SSM parameter name"
    assert cfg.serving_role_for(Tenant("alice")) == "arn:serving-alice"
    assert cfg.serving_role_for(Tenant("carol")) == "arn:s", "the live role as the fallback"
    assert cfg.runtime_role_for(Tenant("alice")) == "arn:agentcore-alice"
    assert cfg.tenant_api_keys == {"alice": "arn:secret-alice"}
    assert cfg.network_configuration() == {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": ["subnet-1", "subnet-2"], "securityGroups": ["sg-1"]},
    }
    monkeypatch.setenv("NW_AWS_PIPELINE_IMAGE", "mine:1")
    assert (
        platform.AwsPlatformConfig.from_settings(
            Settings(track=Track.AWS, _env_file=None), out
        ).pipeline_image
        == "mine:1"
    )
    monkeypatch.setenv("NW_AWS_DATA_BUCKET", "override")
    assert (
        platform.AwsPlatformConfig.from_settings(
            Settings(track=Track.AWS, _env_file=None), out
        ).data_bucket
        == "override"
    )


def test_register_uploads_the_monitor_baseline_beside_the_model(cfg, tenant, tmp_path):
    from tests.platform.aws_fakes import FakeS3, FakeSageMaker

    art = tmp_path / "triage"
    art.mkdir()
    (art / "metadata.json").write_text(json.dumps({"version": "v1", "model_sha256": "f00d"}))
    (art / "data_profile.json").write_text(
        json.dumps(
            {
                "n": 10,
                "text_length_bins": [0, 50, 100],
                "text_length_hist": [0.5, 0.5],
                "data_sha256_12": "abc",
            }
        )
    )
    s3, sm = FakeS3(), FakeSageMaker()
    v = platform.SageMakerRegistry(sm, s3, cfg).register(tenant, "triage", art, {}, {})
    folder = v.uri.removeprefix("s3://northwind-artifacts/").rsplit("/", 1)[0]
    keys = {k for _, k in s3.objects}
    assert {f"{folder}/baseline/statistics.json", f"{folder}/baseline/constraints.json"} <= keys
    [package] = sm.groups["northwind-alice-triage"]
    assert package["CustomerMetadataProperties"]["model_sha256"] == "f00d"


def test_agents_deploy_uses_the_tenants_role_its_key_and_the_vpc(cfg, tenant):
    from dataclasses import replace

    vpc = replace(
        cfg,
        runtime_role_arns={"alice": "arn:aws:iam::123456789012:role/northwind-alice-agentcore"},
        tenant_api_keys={"alice": "arn:aws:secretsmanager:us-east-1:123456789012:secret:k"},
        agent_network_mode="VPC",
        agent_subnets=("subnet-1",),
        agent_security_group="sg-1",
    )
    control = client("bedrock-agentcore-control")
    with Stubber(control) as st:
        st.add_response("list_agent_runtimes", {"agentRuntimes": []}, {"maxResults": 100})
        st.add_response(
            "create_agent_runtime",
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeId": "northwind_alice_resolver-abc",
                "agentRuntimeVersion": "1",
                "createdAt": "2026-09-29T00:00:00Z",
                "status": "CREATING",
            },
            {
                "agentRuntimeName": "northwind_alice_resolver",
                "agentRuntimeArtifact": ANY,
                "roleArn": "arn:aws:iam::123456789012:role/northwind-alice-agentcore",
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]},
                },
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "environmentVariables": ANY,
                "description": ANY,
                "clientToken": ANY,
            },
        )
        agents = platform.AgentCoreRuntime(
            control, client("bedrock-agentcore"), client("agent-registry-control"), vpc
        )
        agents.deploy(tenant, "img@sha256:abc", {}, version="v1")
        st.assert_no_pending_responses()
    with pytest.raises(ValueError, match="AgentSubnets"):
        replace(vpc, agent_subnets=()).network_configuration()


def test_the_pipelines_image_is_read_from_its_ssm_parameter(cfg, tenant):
    from dataclasses import replace

    class Ssm:
        def get_parameter(self, Name):  # noqa: N803
            assert Name == "/northwind/images/pipelines"
            return {"Parameter": {"Value": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/p@sha256:1"}}

    runner = platform.SageMakerPipelines(
        client("sagemaker"),
        client("logs"),
        replace(cfg, pipeline_image="/northwind/images/pipelines"),
        ssm=Ssm(),
    )
    config = runner.pipeline_config(tenant, {}, "triage")
    assert config.image_uri.endswith("/p@sha256:1")
    assert config.role_arn == f"arn:aws:iam::{ACCOUNT}:role/northwind-alice-sagemaker"
