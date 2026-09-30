"""Deploy a model package the moment it is approved.

EventBridge delivers `SageMaker Model Package State Change` events whose `ModelApprovalStatus`
is `Approved` for the package groups of this environment (exact names in the rule). The group
name says whose model it is and for which project (`northwind[-<env>]-<owner>-<project>`); the
target is chosen by the owner:

- a tenant: a Serverless Inference endpoint `northwind-<tenant>-<project>` (pay per request,
  nothing idle), created or updated all at once, served with the tenant's own serving role;
- `live`: the real-time endpoint `northwind-live-<project>` with data capture on, a blue/green
  canary (10 percent for 5 minutes, then the rest, rolled back on the alarms named in the
  environment), a scaling target with a target-tracking policy, and a Model Monitor data
  quality schedule reading the captured requests hourly against the baseline.

Before anything is created the package is checked (`validate`): every container image must
start with an allowed prefix (this account's platform repositories or the listed framework
images) and every model data URL must sit under the owner's prefix in the artifacts bucket
(`tenants/<owner>/`; a live package carries the promoting tenant's URL, which must be under
some `tenants/<tenant>/`). A package that fails is refused and logged, nothing is deployed.

A live promotion copies the artifact to `live/<project>/<stamp>/model.tar.gz` and the baseline
the tenant's registration wrote beside it (`baseline/statistics.json`, `constraints.json`) to
`monitoring/baselines/<endpoint>/`, both platform-owned prefixes, and serves the copy with the
live serving role; a tenant cannot rewrite what the live endpoint serves or monitors against.
A live package without a baseline still deploys; the schedule is skipped and the
`-monitor-silent` alarm says so.

The model is created from an explicit container (the package's image and data URL, the latter
copied for live) so the platform can inject `NW_MODEL_SHA256`, the model file's hash the
registration recorded in the package metadata (`model_sha256`), which the inference handler
checks before it loads anything (`nw/serving/sagemaker/inference.py`).

CodeDeploy has no SageMaker endpoint platform; the endpoint canary is SageMaker's own
deployment guardrail (`DeploymentConfig.BlueGreenUpdatePolicy`, canary traffic shifting), driven
from here. Everything is idempotent: an endpoint that exists is updated, a schedule that exists
is left alone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

PREFIX = os.environ["NW_PREFIX"]
OWNERS = set(os.environ.get("NW_OWNERS", "live").split(","))
PROJECTS = set(os.environ.get("NW_PROJECTS", "triage,semantic").split(","))
SERVING_ROLES = json.loads(os.environ["NW_SERVING_ROLE_ARNS"])  # owner -> role arn
BUCKET = os.environ["NW_ARTIFACTS_BUCKET"]
ALLOWED_IMAGES = tuple(json.loads(os.environ["NW_ALLOWED_IMAGES"]))
CAPTURE_URI = os.environ["NW_CAPTURE_URI"]  # s3://artifacts/capture
MONITOR_URI = os.environ["NW_MONITOR_URI"]  # s3://artifacts/monitoring
PREPROCESSOR_URI = os.environ["NW_PREPROCESSOR_URI"]
MONITOR_IMAGE = os.environ["NW_MONITOR_IMAGE"]
ROLLBACK_ALARMS = json.loads(os.environ.get("NW_ROLLBACK_ALARMS", "{}"))  # project -> [names]
LIVE_INSTANCE = os.environ.get("NW_LIVE_INSTANCE", "ml.m5.large")
MONITOR_INSTANCE = os.environ.get("NW_MONITOR_INSTANCE", "ml.m5.large")
SERVERLESS_MEMORY = int(os.environ.get("NW_SERVERLESS_MEMORY_MB", "3072"))
KMS_KEY = os.environ.get("NW_KMS_KEY_ARN")
LIVE = "live"

sagemaker = boto3.client("sagemaker")
autoscaling = boto3.client("application-autoscaling")
s3 = boto3.client("s3")


class Refused(ValueError):
    """The package is not deployable by this platform."""


def parse_group(name: str) -> tuple[str, str] | None:
    """`northwind-alice-triage` -> ("alice", "triage"); None when not one of ours."""
    if not name.startswith(PREFIX + "-"):
        return None
    rest = name[len(PREFIX) + 1 :]
    if "-" not in rest:
        return None
    owner, project = rest.rsplit("-", 1)
    if owner not in OWNERS or project not in PROJECTS:
        return None
    return owner, project


def _split(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise Refused(f"model data {uri!r} is not an S3 URI")
    bucket, _, key = uri[len("s3://") :].partition("/")
    return bucket, key


def validate(owner: str, containers: list[dict[str, Any]]) -> None:
    """Every image from the allow-list, every model data URL under the owner's prefix."""
    if not containers:
        raise Refused("package has no containers")
    for c in containers:
        image = c.get("Image", "")
        if not image.startswith(ALLOWED_IMAGES):
            raise Refused(f"image {image!r} is not on the allow-list")
        data = c.get("ModelDataUrl")
        if not data:
            continue
        bucket, key = _split(data)
        if bucket != BUCKET or ".." in key:
            raise Refused(f"model data {data!r} is outside the platform's artifacts bucket")
        parts = key.split("/")
        if owner == LIVE:
            ok = len(parts) > 2 and parts[0] == "tenants" and parts[1] in OWNERS - {LIVE}
        else:
            ok = len(parts) > 2 and parts[0] == "tenants" and parts[1] == owner
        if not ok:
            raise Refused(f"model data {data!r} is outside {owner}'s prefix")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    detail = event.get("detail", {})
    if detail.get("ModelApprovalStatus") != "Approved":
        return {"skipped": "not an approval"}
    parsed = parse_group(detail.get("ModelPackageGroupName", ""))
    if parsed is None:
        return {"skipped": "not a platform package group"}
    owner, project = parsed
    package_arn = detail["ModelPackageArn"]
    version = str(detail.get("ModelPackageVersion", ""))
    package = sagemaker.describe_model_package(ModelPackageName=package_arn)
    containers = (package.get("InferenceSpecification") or {}).get("Containers", [])
    try:
        validate(owner, containers)
    except Refused as exc:
        log.error("refused %s: %s", package_arn, exc)
        return {"refused": str(exc), "package": package_arn}
    endpoint = f"{PREFIX}-{owner}-{project}"
    stamp = time.strftime("%Y%m%d%H%M%S")
    model_name = f"{endpoint}-v{version}-{stamp}"
    config_name = f"{endpoint}-v{version}-{stamp}"
    tags = [
        {"Key": "nw:tenant", "Value": owner},
        {"Key": "nw:project", "Value": project},
        {"Key": "nw:model_package", "Value": package_arn},
    ]
    primary = container_for(containers[0], package.get("CustomerMetadataProperties") or {})
    source = primary.get("ModelDataUrl", "")
    if owner == LIVE:
        primary["ModelDataUrl"] = _copy_to_live(source, project, stamp)
    sagemaker.create_model(
        ModelName=model_name,
        PrimaryContainer=primary,
        ExecutionRoleArn=SERVING_ROLES[owner],
        Tags=tags,
    )
    if owner == LIVE:
        has_baseline = _copy_baseline(source, endpoint)
        _live(endpoint, project, model_name, config_name, tags, has_baseline)
    else:
        _serverless(endpoint, model_name, config_name, tags)
    return {"endpoint": endpoint, "model": model_name, "config": config_name, "owner": owner}


def container_for(container: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
    """The package's image and data URL, with the model hash from the registry in the env."""
    out: dict[str, Any] = {"Image": container["Image"]}
    if container.get("ModelDataUrl"):
        out["ModelDataUrl"] = container["ModelDataUrl"]
    env = dict(container.get("Environment") or {})
    sha = metadata.get("model_sha256") or metadata.get("nw:model_sha256")
    if sha:
        env["NW_MODEL_SHA256"] = sha
    if env:
        out["Environment"] = env
    return out


def _copy_to_live(source: str, project: str, stamp: str) -> str:
    bucket, key = _split(source)
    target = f"live/{project}/{stamp}/model.tar.gz"
    extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": KMS_KEY} if KMS_KEY else {}
    s3.copy({"Bucket": bucket, "Key": key}, BUCKET, target, ExtraArgs=extra or None)
    log.info("copied %s to s3://%s/%s", source, BUCKET, target)
    return f"s3://{BUCKET}/{target}"


def _copy_baseline(source: str, endpoint: str) -> bool:
    """`<artifact dir>/baseline/{statistics,constraints}.json` to `monitoring/baselines/<endpoint>/`."""
    bucket, key = _split(source)
    folder = key.rsplit("/", 1)[0]
    extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": KMS_KEY} if KMS_KEY else {}
    for name in ("statistics.json", "constraints.json"):
        try:
            s3.copy(
                {"Bucket": bucket, "Key": f"{folder}/baseline/{name}"},
                BUCKET,
                f"monitoring/baselines/{endpoint}/{name}",
                ExtraArgs=extra or None,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                log.warning("no Model Monitor baseline beside %s; schedule skipped", source)
                return False
            raise
    return True


def _exists(endpoint: str) -> bool:
    try:
        sagemaker.describe_endpoint(EndpointName=endpoint)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("ValidationException",):
            return False
        raise


def _serverless(endpoint: str, model_name: str, config_name: str, tags: list) -> None:
    sagemaker.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": model_name,
                "ServerlessConfig": {"MemorySizeInMB": SERVERLESS_MEMORY, "MaxConcurrency": 5},
            }
        ],
        Tags=tags,
        **({"KmsKeyId": KMS_KEY} if KMS_KEY else {}),
    )
    if _exists(endpoint):
        sagemaker.update_endpoint(EndpointName=endpoint, EndpointConfigName=config_name)
        log.info("updating serverless endpoint %s to %s", endpoint, config_name)
    else:
        sagemaker.create_endpoint(EndpointName=endpoint, EndpointConfigName=config_name, Tags=tags)
        log.info("creating serverless endpoint %s", endpoint)


def _live(
    endpoint: str, project: str, model_name: str, config_name: str, tags: list, baseline: bool
) -> None:
    sagemaker.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": model_name,
                "InitialInstanceCount": 1,
                "InstanceType": LIVE_INSTANCE,
                "InitialVariantWeight": 1.0,
            }
        ],
        DataCaptureConfig={
            "EnableCapture": True,
            "InitialSamplingPercentage": 100,
            "DestinationS3Uri": f"{CAPTURE_URI}/{endpoint}",
            "CaptureOptions": [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}],
            "CaptureContentTypeHeader": {"JsonContentTypes": ["application/json"]},
            **({"KmsKeyId": KMS_KEY} if KMS_KEY else {}),
        },
        Tags=tags,
        **({"KmsKeyId": KMS_KEY} if KMS_KEY else {}),
    )
    deployment = {
        "BlueGreenUpdatePolicy": {
            "TrafficRoutingConfiguration": {
                "Type": "CANARY",
                "CanarySize": {"Type": "CAPACITY_PERCENT", "Value": 10},
                "WaitIntervalInSeconds": 300,
            },
            "TerminationWaitInSeconds": 120,
            "MaximumExecutionTimeoutInSeconds": 3600,
        },
        "AutoRollbackConfiguration": {
            "Alarms": [{"AlarmName": n} for n in ROLLBACK_ALARMS.get(project, [])]
        },
    }
    if _exists(endpoint):
        sagemaker.update_endpoint(
            EndpointName=endpoint, EndpointConfigName=config_name, DeploymentConfig=deployment
        )
        log.info("canary update of %s to %s", endpoint, config_name)
    else:
        # The first deploy has no blue fleet to shift from: create, then scale.
        sagemaker.create_endpoint(EndpointName=endpoint, EndpointConfigName=config_name, Tags=tags)
        log.info("creating live endpoint %s", endpoint)
        _autoscale(endpoint)
    if baseline:
        _monitor(endpoint, project)


def _autoscale(endpoint: str) -> None:
    resource = f"endpoint/{endpoint}/variant/AllTraffic"
    autoscaling.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=resource,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        MinCapacity=1,
        MaxCapacity=3,
    )
    autoscaling.put_scaling_policy(
        PolicyName=f"{endpoint}-invocations",
        ServiceNamespace="sagemaker",
        ResourceId=resource,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": 200.0,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "SageMakerVariantInvocationsPerInstance"
            },
            "ScaleInCooldown": 300,
            "ScaleOutCooldown": 60,
        },
    )


def _not_found(exc: ClientError) -> bool:
    code = exc.response["Error"]["Code"]
    return code in ("ResourceNotFound", "ValidationException") or "not find" in str(exc).lower()


def _monitor(endpoint: str, project: str) -> None:
    """A data quality job definition and an hourly schedule; both left alone when present."""
    definition = f"{endpoint}-data-quality"
    schedule = f"{endpoint}-data-quality"
    baselines = f"{MONITOR_URI}/baselines/{endpoint}"
    try:
        sagemaker.describe_data_quality_job_definition(JobDefinitionName=definition)
    except ClientError as exc:
        if not _not_found(exc):
            raise
        sagemaker.create_data_quality_job_definition(
            JobDefinitionName=definition,
            DataQualityBaselineConfig={
                "ConstraintsResource": {"S3Uri": f"{baselines}/constraints.json"},
                "StatisticsResource": {"S3Uri": f"{baselines}/statistics.json"},
            },
            DataQualityAppSpecification={
                "ImageUri": MONITOR_IMAGE,
                "RecordPreprocessorSourceUri": PREPROCESSOR_URI,
                "Environment": {"publish_cloudwatch_metrics": "Enabled"},
            },
            DataQualityJobInput={
                "EndpointInput": {
                    "EndpointName": endpoint,
                    "LocalPath": "/opt/ml/processing/input/endpoint",
                    "S3InputMode": "File",
                    "S3DataDistributionType": "FullyReplicated",
                }
            },
            DataQualityJobOutputConfig={
                "MonitoringOutputs": [
                    {
                        "S3Output": {
                            "S3Uri": f"{MONITOR_URI}/reports/{endpoint}",
                            "LocalPath": "/opt/ml/processing/output",
                            "S3UploadMode": "EndOfJob",
                        }
                    }
                ],
                **({"KmsKeyId": KMS_KEY} if KMS_KEY else {}),
            },
            JobResources={
                "ClusterConfig": {
                    "InstanceCount": 1,
                    "InstanceType": MONITOR_INSTANCE,
                    "VolumeSizeInGB": 20,
                }
            },
            StoppingCondition={"MaxRuntimeInSeconds": 1800},
            RoleArn=SERVING_ROLES[LIVE],
            Tags=[{"Key": "nw:project", "Value": project}, {"Key": "nw:tenant", "Value": LIVE}],
        )
    try:
        sagemaker.describe_monitoring_schedule(MonitoringScheduleName=schedule)
        return
    except ClientError as exc:
        if not _not_found(exc):
            raise
    sagemaker.create_monitoring_schedule(
        MonitoringScheduleName=schedule,
        MonitoringScheduleConfig={
            "ScheduleConfig": {"ScheduleExpression": "cron(0 * ? * * *)"},
            "MonitoringType": "DataQuality",
            "MonitoringJobDefinitionName": definition,
        },
        Tags=[{"Key": "nw:project", "Value": project}, {"Key": "nw:tenant", "Value": LIVE}],
    )
