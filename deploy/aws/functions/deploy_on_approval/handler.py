"""Deploy a model package the moment it is approved.

EventBridge delivers `SageMaker Model Package State Change` events whose `ModelApprovalStatus`
is `Approved`. The package group name says whose model it is and for which project
(`northwind[-<env>]-<owner>-<project>`); the target is chosen by the owner:

- a tenant: a Serverless Inference endpoint `northwind-<tenant>-<project>` (pay per request,
  nothing idle), created or updated all at once;
- `live`: the real-time endpoint `northwind-live-<project>` with data capture on, a blue/green
  canary (10 percent for 5 minutes, then the rest, rolled back on the alarms named in the
  environment), a scaling target with a target-tracking policy, and a Model Monitor data
  quality schedule reading the captured requests hourly against the baseline the pipeline
  wrote to `baselines/<project>/`.

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
SERVING_ROLE = os.environ["NW_SERVING_ROLE_ARN"]
CAPTURE_URI = os.environ["NW_CAPTURE_URI"]  # s3://artifacts/capture
BASELINE_URI = os.environ["NW_BASELINE_URI"]  # s3://artifacts/baselines
MONITOR_URI = os.environ["NW_MONITOR_URI"]  # s3://artifacts/monitoring
MONITOR_IMAGE = os.environ["NW_MONITOR_IMAGE"]
ROLLBACK_ALARMS = json.loads(os.environ.get("NW_ROLLBACK_ALARMS", "{}"))  # project -> [names]
LIVE_INSTANCE = os.environ.get("NW_LIVE_INSTANCE", "ml.m5.large")
SERVERLESS_MEMORY = int(os.environ.get("NW_SERVERLESS_MEMORY_MB", "3072"))
KMS_KEY = os.environ.get("NW_KMS_KEY_ARN")

sagemaker = boto3.client("sagemaker")
autoscaling = boto3.client("application-autoscaling")


def parse_group(name: str) -> tuple[str, str] | None:
    """`northwind-alice-triage` -> ("alice", "triage"); None when not ours."""
    if not name.startswith(PREFIX + "-"):
        return None
    rest = name[len(PREFIX) + 1 :]
    if "-" not in rest:
        return None
    owner, project = rest.rsplit("-", 1)
    return owner, project


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
    endpoint = f"{PREFIX}-{owner}-{project}"
    stamp = time.strftime("%Y%m%d%H%M%S")
    model_name = f"{endpoint}-v{version}-{stamp}"
    config_name = f"{endpoint}-v{version}-{stamp}"
    tags = [
        {"Key": "nw:tenant", "Value": owner},
        {"Key": "nw:project", "Value": project},
        {"Key": "nw:model_package", "Value": package_arn},
    ]
    sagemaker.create_model(
        ModelName=model_name,
        PrimaryContainer={"ModelPackageName": package_arn},
        ExecutionRoleArn=SERVING_ROLE,
        Tags=tags,
    )
    if owner == "live":
        _live(endpoint, project, model_name, config_name, tags)
    else:
        _serverless(endpoint, model_name, config_name, tags)
    return {"endpoint": endpoint, "model": model_name, "config": config_name, "owner": owner}


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


def _live(endpoint: str, project: str, model_name: str, config_name: str, tags: list) -> None:
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
        return
    # The first deploy has no blue fleet to shift from: create, then scale and monitor.
    sagemaker.create_endpoint(EndpointName=endpoint, EndpointConfigName=config_name, Tags=tags)
    log.info("creating live endpoint %s", endpoint)
    _autoscale(endpoint)
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


def _monitor(endpoint: str, project: str) -> None:
    schedule = f"{endpoint}-data-quality"
    try:
        sagemaker.describe_monitoring_schedule(MonitoringScheduleName=schedule)
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFound":
            if "not find" not in str(exc).lower():
                raise
    sagemaker.create_monitoring_schedule(
        MonitoringScheduleName=schedule,
        MonitoringScheduleConfig={
            "ScheduleConfig": {"ScheduleExpression": "cron(0 * ? * * *)"},
            "MonitoringType": "DataQuality",
            "MonitoringJobDefinition": {
                "BaselineConfig": {
                    "ConstraintsResource": {"S3Uri": f"{BASELINE_URI}/{project}/constraints.json"},
                    "StatisticsResource": {"S3Uri": f"{BASELINE_URI}/{project}/statistics.json"},
                },
                "MonitoringInputs": [
                    {
                        "EndpointInput": {
                            "EndpointName": endpoint,
                            "LocalPath": "/opt/ml/processing/input",
                            "S3InputMode": "File",
                            "S3DataDistributionType": "FullyReplicated",
                        }
                    }
                ],
                "MonitoringOutputConfig": {
                    "MonitoringOutputs": [
                        {
                            "S3Output": {
                                "S3Uri": f"{MONITOR_URI}/{endpoint}",
                                "LocalPath": "/opt/ml/processing/output",
                                "S3UploadMode": "EndOfJob",
                            }
                        }
                    ],
                    **({"KmsKeyId": KMS_KEY} if KMS_KEY else {}),
                },
                "MonitoringResources": {
                    "ClusterConfig": {
                        "InstanceCount": 1,
                        "InstanceType": "ml.m5.xlarge",
                        "VolumeSizeInGB": 20,
                    }
                },
                "MonitoringAppSpecification": {"ImageUri": MONITOR_IMAGE},
                "StoppingCondition": {"MaxRuntimeInSeconds": 1800},
                "RoleArn": SERVING_ROLE,
            },
        },
        Tags=[{"Key": "nw:project", "Value": project}, {"Key": "nw:tenant", "Value": "live"}],
    )
