"""The AWS platform: SageMaker Model Registry, SageMaker Pipelines, SageMaker endpoints, Bedrock
Prompt Management, Bedrock Knowledge Bases and AgentCore, behind the six protocols of
`nw/platform/base.py`.

boto3 is imported lazily so the other tracks never need it. Every client class takes its boto3
clients as constructor arguments, which is what the tests stub; `build(settings)` wires the real
ones from the environment and, when present, `deploy/aws/outputs.json` (written by
`make deploy-aws`).

    uv run python -m nw.platform.aws prompts-catalog   # deploy/aws/prompts/catalog.json
    uv run python -m nw.platform.aws describe           # what build(settings) resolved
    uv run python -m nw.platform.aws pipeline-upsert triage   # create or update the pipeline

Names follow `Tenant.prefix`: the model package group `northwind-alice-triage`, the endpoint
`northwind-alice-triage`, the pipeline `northwind-alice-triage`, the prompt
`northwind-alice-policy-answer`, the runtime `northwind_alice_resolver`. `NW_ENVIRONMENT` is the
stack's prefix (`northwind` or `northwind-<env>`).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nw.config import Settings, Track
from nw.llm.prompts import prompt_hash
from nw.platform.base import (
    Hit,
    ModelVersion,
    PipelineRun,
    Platform,
    PromptVersion,
    RunStatus,
    Stage,
    Tenant,
)

LIVE = "live"
OUTPUTS = Path(__file__).resolve().parents[2] / "deploy" / "aws" / "outputs.json"
CATALOG = Path(__file__).resolve().parents[2] / "deploy" / "aws" / "prompts" / "catalog.json"

APPROVAL = {
    Stage.CANDIDATE: "PendingManualApproval",
    Stage.APPROVED: "Approved",
    Stage.LIVE: "Approved",
    Stage.RETIRED: "Rejected",
}
STAGE_OF = {
    "PendingManualApproval": Stage.CANDIDATE,
    "Approved": Stage.APPROVED,
    "Rejected": Stage.RETIRED,
}
RUN_STATUS = {
    "Executing": RunStatus.RUNNING,
    "Stopping": RunStatus.RUNNING,
    "Stopped": RunStatus.STOPPED,
    "Failed": RunStatus.FAILED,
    "Succeeded": RunStatus.SUCCEEDED,
}


# ----- configuration ---------------------------------------------------------------------


@dataclass
class AwsPlatformConfig:
    """What the client needs to know about the deployed stack. Environment variables win;
    `deploy/aws/outputs.json` fills the rest after `make deploy-aws`."""

    region: str
    environment: str = "northwind"
    data_bucket: str = ""
    artifacts_bucket: str = ""
    serving_role_arn: str = ""
    runtime_role_arn: str = ""
    registry_id: str = ""
    gateway_url: str | None = None
    knowledge_bases: dict[str, str] = field(default_factory=dict)  # owner -> knowledge base id
    data_sources: dict[str, str] = field(default_factory=dict)  # owner -> data source id
    images: dict[str, str] = field(default_factory=dict)  # project -> inference image URI
    direct_deploy: bool = False  # solo: the client creates endpoints itself
    # The pipeline definition: the nw-pipelines image in ECR (NW_AWS_PIPELINE_IMAGE or
    # NW_AWS_IMAGE_PIPELINES, else the stack's `PipelineImage` output) and the role its jobs
    # run as (NW_AWS_PIPELINE_ROLE_ARN; default the tenant's execution role
    # `<prefix>-sagemaker` in the image's account).
    pipeline_image: str = ""
    pipeline_role_arn: str = ""

    @classmethod
    def from_settings(cls, settings: Settings, outputs: Path = OUTPUTS) -> AwsPlatformConfig:
        out = _read_outputs(outputs)
        env = os.environ
        environment = (
            getattr(settings, "environment", None) or out.get("Environment") or "northwind"
        )
        kbs = dict(_pairs(env.get("NW_AWS_KNOWLEDGE_BASES") or out.get("KnowledgeBases", "")))
        if env.get("NW_KNOWLEDGE_BASE_ID"):
            kbs.setdefault(LIVE, env["NW_KNOWLEDGE_BASE_ID"])
        images = {
            k[len("NW_AWS_IMAGE_") :].lower(): v
            for k, v in env.items()
            if k.startswith("NW_AWS_IMAGE_")
        }
        tenant = getattr(settings, "tenant", None) or "solo"
        return cls(
            region=settings.aws_region,
            environment=environment,
            data_bucket=env.get("NW_AWS_DATA_BUCKET") or out.get("DataBucket", ""),
            artifacts_bucket=env.get("NW_AWS_ARTIFACTS_BUCKET") or out.get("ArtifactsBucket", ""),
            serving_role_arn=env.get("NW_AWS_SERVING_ROLE_ARN") or out.get("ServingRoleArn", ""),
            runtime_role_arn=env.get("NW_AWS_RUNTIME_ROLE_ARN") or out.get("RuntimeRoleArn", ""),
            registry_id=env.get("NW_AWS_REGISTRY_ID") or out.get("RegistryId", ""),
            gateway_url=getattr(settings, "gateway_url", None) or out.get("GatewayUrl") or None,
            knowledge_bases=kbs,
            data_sources=dict(_pairs(env.get("NW_AWS_DATA_SOURCES", ""))),
            images=images,
            direct_deploy=(env.get("NW_AWS_DIRECT_DEPLOY", "").lower() in ("1", "true"))
            or (tenant == "solo" and env.get("NW_AWS_DIRECT_DEPLOY", "") == ""),
            pipeline_image=env.get("NW_AWS_PIPELINE_IMAGE")
            or images.get("pipelines")
            or out.get("PipelineImage", ""),
            pipeline_role_arn=env.get("NW_AWS_PIPELINE_ROLE_ARN") or "",
        )

    def under(self) -> str:
        return self.environment.replace("-", "_")


def _read_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    for key, value in data.items():
        if key.startswith("northwind") and isinstance(value, dict):
            return value
    return data if all(isinstance(v, str) for v in data.values()) else {}


def _pairs(text: str) -> Iterator[tuple[str, str]]:
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            yield k.strip(), v.strip()


# ----- Model Registry: SageMaker Model Registry ------------------------------------------


class SageMakerRegistry:
    """Model package groups per tenant and project; approval status is the stage.

    LIVE is a promotion: the approved package is re-registered, already approved, in the
    `<environment>-live-<name>` group, which the platform's approval Lambda deploys to the live
    endpoint with a canary. The tenant's own package keeps `nw:stage=live` in its metadata."""

    def __init__(self, sagemaker: Any, s3: Any, config: AwsPlatformConfig) -> None:
        self.sm = sagemaker
        self.s3 = s3
        self.cfg = config

    def register(
        self,
        tenant: Tenant,
        name: str,
        artifact: Path,
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
        serving_image: str | None = None,
    ) -> ModelVersion:
        from nw.serving.sagemaker import IMAGES

        image = serving_image or tags.get("image") or self.cfg.images.get(name) or IMAGES.get(name)
        if not image:
            raise ValueError(
                f"no inference image for {name!r}: pass tags['image'] "
                f"or set NW_AWS_IMAGE_{name.upper()}"
            )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        key = f"tenants/{tenant.name}/{name}/{stamp}/model.tar.gz"
        self.s3.put_object(Bucket=self.cfg.artifacts_bucket, Key=key, Body=_tarball(artifact))
        metrics_key = f"tenants/{tenant.name}/{name}/{stamp}/metrics.json"
        self.s3.put_object(
            Bucket=self.cfg.artifacts_bucket,
            Key=metrics_key,
            Body=json.dumps({"metrics": dict(metrics)}).encode(),
        )
        uri = f"s3://{self.cfg.artifacts_bucket}/{key}"
        metadata = {**{k: str(v) for k, v in tags.items() if k != "image"}}
        metadata.update({f"metric:{k}": f"{v:.6g}" for k, v in metrics.items()})
        metadata["nw:tenant"] = tenant.name
        response = self.sm.create_model_package(
            ModelPackageGroupName=tenant.resource(name),
            ModelPackageDescription=f"{name} trained by {tenant.name} at {stamp}",
            InferenceSpecification=_inference_spec(image, uri),
            ModelApprovalStatus="PendingManualApproval",
            CustomerMetadataProperties=metadata,
            ModelMetrics={
                "ModelQuality": {
                    "Statistics": {
                        "ContentType": "application/json",
                        "S3Uri": f"s3://{self.cfg.artifacts_bucket}/{metrics_key}",
                    }
                }
            },
            Tags=[{"Key": "nw:tenant", "Value": tenant.name}, {"Key": "nw:project", "Value": name}],
        )
        arn = response["ModelPackageArn"]
        return ModelVersion(
            name=name,
            version=arn.rsplit("/", 1)[-1],
            stage=Stage.CANDIDATE,
            uri=uri,
            metrics=dict(metrics),
            tags={**tags, "arn": arn},
        )

    def set_stage(
        self, tenant: Tenant, name: str, version: str, stage: Stage, reason: str
    ) -> ModelVersion:
        arn = self._arn(tenant, name, version)
        described = self.sm.describe_model_package(ModelPackageName=arn)
        metadata = dict(described.get("CustomerMetadataProperties") or {})
        metadata["nw:stage"] = stage.value
        self.sm.update_model_package(
            ModelPackageArn=arn,
            ModelApprovalStatus=APPROVAL[stage],
            ApprovalDescription=reason[:1024],
            CustomerMetadataProperties=metadata,
        )
        if stage == Stage.LIVE:
            live_group = f"{self.cfg.environment}-{LIVE}-{name}"
            self.sm.create_model_package(
                ModelPackageGroupName=live_group,
                ModelPackageDescription=f"promoted from {arn}: {reason}"[:1024],
                InferenceSpecification=described["InferenceSpecification"],
                ModelApprovalStatus="Approved",
                CustomerMetadataProperties={
                    **metadata,
                    "nw:source": arn,
                    "nw:promoted_by": tenant.name,
                },
                Tags=[{"Key": "nw:tenant", "Value": LIVE}, {"Key": "nw:project", "Value": name}],
            )
        return self._version(name, self.sm.describe_model_package(ModelPackageName=arn))

    def versions(self, tenant: Tenant, name: str) -> Sequence[ModelVersion]:
        out: list[ModelVersion] = []
        token: str | None = None
        while True:
            kw = {"NextToken": token} if token else {}
            page = self.sm.list_model_packages(
                ModelPackageGroupName=tenant.resource(name),
                SortBy="CreationTime",
                SortOrder="Descending",
                **kw,
            )
            for summary in page["ModelPackageSummaryList"]:
                out.append(
                    self._version(
                        name,
                        self.sm.describe_model_package(ModelPackageName=summary["ModelPackageArn"]),
                    )
                )
            token = page.get("NextToken")
            if not token:
                return out

    def live(self, tenant: Tenant, name: str) -> ModelVersion | None:
        for v in self.versions(tenant, name):
            if v.stage in (Stage.LIVE, Stage.APPROVED):
                return v
        return None

    def download(self, tenant: Tenant, version: ModelVersion, into: Path) -> Path:
        bucket, key = version.uri.removeprefix("s3://").split("/", 1)
        body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        into.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            tar.extractall(into, filter="data")
        return into

    def _arn(self, tenant: Tenant, name: str, version: str) -> str:
        if version.startswith("arn:"):
            return version
        region = self.cfg.region
        account = self.cfg.serving_role_arn.split(":")[4] if self.cfg.serving_role_arn else None
        if account:
            group = tenant.resource(name)
            return f"arn:aws:sagemaker:{region}:{account}:model-package/{group}/{version}"
        page = self.sm.list_model_packages(
            ModelPackageGroupName=tenant.resource(name), MaxResults=100
        )
        for summary in page["ModelPackageSummaryList"]:
            if str(summary["ModelPackageVersion"]) == version:
                return summary["ModelPackageArn"]
        raise KeyError(f"{tenant.resource(name)} has no version {version}")

    @staticmethod
    def _version(name: str, described: Mapping[str, Any]) -> ModelVersion:
        metadata = dict(described.get("CustomerMetadataProperties") or {})
        stage = STAGE_OF.get(described.get("ModelApprovalStatus", ""), Stage.CANDIDATE)
        if metadata.get("nw:stage") == Stage.LIVE.value and stage == Stage.APPROVED:
            stage = Stage.LIVE
        containers = described.get("InferenceSpecification", {}).get("Containers", [{}])
        metrics = {
            k[len("metric:") :]: float(v) for k, v in metadata.items() if k.startswith("metric:")
        }
        tags = {k: v for k, v in metadata.items() if not k.startswith("metric:")}
        tags["arn"] = described["ModelPackageArn"]
        return ModelVersion(
            name=name,
            version=str(
                described.get(
                    "ModelPackageVersion", described["ModelPackageArn"].rsplit("/", 1)[-1]
                )
            ),
            stage=stage,
            uri=containers[0].get("ModelDataUrl", "") if containers else "",
            metrics=metrics,
            tags=tags,
        )


def _inference_spec(image: str, uri: str) -> dict[str, Any]:
    return {
        "Containers": [{"Image": image, "ModelDataUrl": uri}],
        "SupportedContentTypes": ["application/json"],
        "SupportedResponseMIMETypes": ["application/json"],
        "SupportedRealtimeInferenceInstanceTypes": ["ml.m5.large", "ml.m5.xlarge"],
    }


def _tarball(artifact: Path) -> bytes:
    """`model.tar.gz` as SageMaker expects it: the artifact's files at the archive root and a
    `code/` directory with `inference.py` for the prebuilt container (nw.serving.sagemaker)."""
    if artifact.is_file() and artifact.name.endswith((".tar.gz", ".tgz")):
        return artifact.read_bytes()
    if artifact.is_dir():
        import tempfile

        from nw.serving.sagemaker.package import package

        try:
            with tempfile.TemporaryDirectory(prefix="nw-pkg-") as tmp:
                return package(artifact, Path(tmp)).read_bytes()
        except FileNotFoundError:
            pass  # not a triage or semantic artifact: plain archive below
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if artifact.is_dir():
            for path in sorted(artifact.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(artifact)))
        else:
            tar.add(artifact, arcname=artifact.name)
    return buf.getvalue()


# ----- Pipelines: SageMaker Pipelines ----------------------------------------------------


def _sagemaker_definition(pipeline: str, config: Any) -> str:
    """The definition JSON from `nw.pipelines.sagemaker`, built without an AWS call."""
    from nw.pipelines.sagemaker import definition

    return json.dumps(definition(pipeline, config))


# Parameters the SageMaker definitions bake in instead of declaring (`Param.sagemaker=False`),
# plus the definition overrides `upsert` reads; never sent to StartPipelineExecution.
_NOT_SAGEMAKER_PARAMS = {
    "tenant",
    "environment",
    "register_model",
    "template_path",
    "image_uri",
    "role_arn",
    "serving_image_uri",
    "bucket",
}


class SageMakerPipelines:
    """Upsert, start, follow and read the logs of the tenant's pipeline `<prefix>-<pipeline>`
    (`northwind-alice-triage`). `submit` upserts the definition from `nw.pipelines.sagemaker`
    first (create when missing, update when present), so a pipeline always runs the code of
    the commit that submitted it; `upsert` alone is `make pipeline-upsert-aws`."""

    def __init__(
        self,
        sagemaker: Any,
        logs: Any,
        config: AwsPlatformConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 15.0,
        definitions: Callable[[str, Any], str] = _sagemaker_definition,
    ) -> None:
        self.sm = sagemaker
        self.cw = logs
        self.cfg = config
        self.sleep = sleep
        self.poll_s = poll_s
        self.definitions = definitions

    def pipeline_config(
        self, tenant: Tenant, params: Mapping[str, Any] | None = None, pipeline: str | None = None
    ) -> Any:
        """The `SageMakerConfig` the definition is built from: the image, role, bucket and
        region from the platform config, each overridable by `params` (`image_uri`,
        `role_arn`, `serving_image_uri`, `bucket`)."""
        from nw.pipelines.sagemaker.definitions import SageMakerConfig

        p = params or {}
        image = p.get("image_uri") or self.cfg.pipeline_image
        if not image:
            raise ValueError(
                "no nw-pipelines image: set NW_AWS_PIPELINE_IMAGE (or NW_AWS_IMAGE_PIPELINES) "
                "to the image URI in ECR"
            )
        role = p.get("role_arn") or self.cfg.pipeline_role_arn
        if not role:
            account = str(image).split(".", 1)[0]
            if not account.isdigit():
                raise ValueError("set NW_AWS_PIPELINE_ROLE_ARN: the image URI names no account")
            role = f"arn:aws:iam::{account}:role/{tenant.resource('sagemaker')}"
        return SageMakerConfig(
            tenant=tenant,
            role_arn=str(role),
            image_uri=str(image),
            serving_image_uri=p.get("serving_image_uri") or None,
            bucket=str(p.get("bucket") or self.cfg.artifacts_bucket or "nw-bucket"),
            region=self.cfg.region,
            defaults=self.deployed_defaults(tenant, pipeline),
        )

    def deployed_defaults(self, tenant: Tenant, pipeline: str | None = None) -> dict[str, str]:
        """The stack's locations for the parameters whose defaults are repo paths: the tickets
        in the data bucket, the tenant's prefix in the artifacts bucket, and the production
        summary the deploy copied to `s3://<artifacts>/baselines/`. Empty for a bucket the
        config does not know, so the repo default stands."""
        from nw.pipelines.params import baseline_uri

        out: dict[str, str] = {}
        if self.cfg.data_bucket:
            out["data_uri"] = f"s3://{self.cfg.data_bucket}/data/tickets/"
        if self.cfg.artifacts_bucket:
            out["output_root"] = f"s3://{self.cfg.artifacts_bucket}/tenants/{tenant.name}/pipelines"
            if pipeline:
                out["production_summary"] = baseline_uri("s3", self.cfg.artifacts_bucket, pipeline)
        return out

    def upsert(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any] | None = None) -> str:
        """Create the tenant's pipeline or update it in place; returns its ARN. Idempotent:
        running it twice leaves one pipeline with the latest definition."""
        from nw.pipelines import canonical

        kind = canonical(pipeline)
        config = self.pipeline_config(tenant, params, kind)
        name = tenant.resource(kind)
        body = {
            "PipelineName": name,
            "PipelineDefinition": self.definitions(kind, config),
            "PipelineDescription": f"Northwind {kind} training for {tenant.prefix}",
            "RoleArn": config.role_arn,
        }
        try:
            self.sm.describe_pipeline(PipelineName=name)
        except self.sm.exceptions.ResourceNotFound:
            response = self.sm.create_pipeline(
                **body,
                PipelineDisplayName=name,
                ClientRequestToken=str(uuid.uuid4()),
                Tags=[
                    {"Key": "tenant", "Value": tenant.name},
                    {"Key": "environment", "Value": tenant.environment},
                ],
            )
        else:
            response = self.sm.update_pipeline(**body)
        return response["PipelineArn"]

    def _parameters(self, tenant: Tenant, kind: str, params: Mapping[str, Any]) -> list[dict]:
        """snake_case to the SageMaker names (`min_p0_recall` to `MinP0Recall`), booleans as
        the `true`/`false` strings the definition compares, and the S3 defaults for the data,
        the output root and the production summary when the stack's buckets are known."""
        from nw.pipelines.params import sagemaker_parameter_name

        values = {k: v for k, v in params.items() if k not in _NOT_SAGEMAKER_PARAMS}
        for key, value in self.deployed_defaults(tenant, kind).items():
            values.setdefault(key, value)
        out = []
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            out.append({"Name": sagemaker_parameter_name(key), "Value": text})
        return out

    def submit(self, tenant: Tenant, pipeline: str, params: Mapping[str, Any]) -> PipelineRun:
        from nw.pipelines import canonical

        kind = canonical(pipeline)
        name = tenant.resource(kind)
        self.upsert(tenant, kind, params)
        response = self.sm.start_pipeline_execution(
            PipelineName=name,
            PipelineExecutionDisplayName=f"{tenant.name}-{time.strftime('%Y%m%d-%H%M%S')}",
            PipelineParameters=self._parameters(tenant, kind, params),
            ClientRequestToken=str(uuid.uuid4()),
        )
        arn = response["PipelineExecutionArn"]
        return PipelineRun(
            pipeline=pipeline, run_id=arn, status=RunStatus.QUEUED, url=self._url(name, arn)
        )

    def status(self, tenant: Tenant, run: PipelineRun) -> PipelineRun:
        described = self.sm.describe_pipeline_execution(PipelineExecutionArn=run.run_id)
        status = RUN_STATUS.get(described["PipelineExecutionStatus"], RunStatus.RUNNING)
        outputs: dict[str, str] = {}
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED):
            for step in self.steps(run):
                meta = step.get("Metadata") or {}
                for body in meta.values():
                    if isinstance(body, Mapping) and body.get("Arn"):
                        outputs[step["StepName"]] = body["Arn"]
                if step.get("FailureReason"):
                    outputs[f"{step['StepName']}:failure"] = step["FailureReason"]
        return PipelineRun(
            pipeline=run.pipeline, run_id=run.run_id, status=status, url=run.url, outputs=outputs
        )

    def wait(self, tenant: Tenant, run: PipelineRun, timeout_s: float = 1800) -> PipelineRun:
        deadline = time.monotonic() + timeout_s
        while True:
            current = self.status(tenant, run)
            if current.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED):
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{run.run_id} still {current.status} after {timeout_s} s")
            self.sleep(self.poll_s)

    def logs(self, tenant: Tenant, run: PipelineRun) -> Iterator[str]:
        for step in self.steps(run):
            yield f"[{step.get('StepStatus', '?')}] {step['StepName']}"
            meta = step.get("Metadata") or {}
            for kind, group in (
                ("TrainingJob", "/aws/sagemaker/TrainingJobs"),
                ("ProcessingJob", "/aws/sagemaker/ProcessingJobs"),
            ):
                arn = (meta.get(kind) or {}).get("Arn")
                if not arn:
                    continue
                job = arn.rsplit("/", 1)[-1]
                token: str | None = None
                while True:
                    kw = {"nextToken": token} if token else {}
                    page = self.cw.filter_log_events(
                        logGroupName=group, logStreamNamePrefix=job, limit=1000, **kw
                    )
                    for event in page.get("events", []):
                        yield f"  {event.get('message', '').rstrip()}"
                    token = page.get("nextToken")
                    if not token:
                        break

    def steps(self, run: PipelineRun) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kw = {"NextToken": token} if token else {}
            page = self.sm.list_pipeline_execution_steps(
                PipelineExecutionArn=run.run_id, SortOrder="Ascending", **kw
            )
            out.extend(page.get("PipelineExecutionSteps", []))
            token = page.get("NextToken")
            if not token:
                return out

    def _url(self, name: str, arn: str) -> str:
        execution = arn.rsplit("/", 1)[-1]
        return (
            f"https://{self.cfg.region}.console.aws.amazon.com/sagemaker/home?region={self.cfg.region}"
            f"#/pipelines/{name}/executions/{execution}"
        )


# ----- Endpoints: SageMaker Serverless Inference and the live real-time endpoint --------


class SageMakerEndpoints:
    """Cohort mode deploys through the approval event (the platform's Lambda creates the
    endpoint); solo mode (`direct_deploy`) creates the serverless endpoint from here."""

    def __init__(
        self, sagemaker: Any, runtime: Any, registry: SageMakerRegistry, config: AwsPlatformConfig
    ) -> None:
        self.sm = sagemaker
        self.runtime = runtime
        self.registry = registry
        self.cfg = config

    def deploy(
        self, tenant: Tenant, version: ModelVersion, *, live: bool = False, canary_percent: int = 0
    ) -> str:
        arn = version.tags.get("arn") or self.registry._arn(tenant, version.name, version.version)
        if live:
            self.registry.set_stage(
                tenant, version.name, arn, Stage.LIVE, f"promoted to live by {tenant.name}"
            )
            return f"{self.cfg.environment}-{LIVE}-{version.name}"
        endpoint = tenant.resource(version.name)
        if not self.cfg.direct_deploy:
            self.registry.set_stage(
                tenant, version.name, arn, Stage.APPROVED, f"approved for {endpoint}"
            )
            return endpoint
        stamp = time.strftime("%Y%m%d%H%M%S")
        model_name = f"{endpoint}-{stamp}"
        self.sm.create_model(
            ModelName=model_name,
            PrimaryContainer={"ModelPackageName": arn},
            ExecutionRoleArn=self.cfg.serving_role_arn,
        )
        self.sm.create_endpoint_config(
            EndpointConfigName=model_name,
            ProductionVariants=[
                {
                    "VariantName": "AllTraffic",
                    "ModelName": model_name,
                    "ServerlessConfig": {"MemorySizeInMB": 3072, "MaxConcurrency": 5},
                }
            ],
        )
        if self.status(tenant, version.name)["status"] == "absent":
            self.sm.create_endpoint(EndpointName=endpoint, EndpointConfigName=model_name)
        else:
            self.sm.update_endpoint(EndpointName=endpoint, EndpointConfigName=model_name)
        return endpoint

    def invoke(self, tenant: Tenant, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.runtime.invoke_endpoint(
            EndpointName=tenant.resource(name),
            ContentType="application/json",
            Accept="application/json",
            Body=json.dumps(payload).encode(),
        )
        return json.loads(response["Body"].read())

    def status(self, tenant: Tenant, name: str) -> Mapping[str, Any]:
        from botocore.exceptions import ClientError

        try:
            described = self.sm.describe_endpoint(EndpointName=tenant.resource(name))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ValidationException":
                return {"status": "absent", "endpoint": tenant.resource(name)}
            raise
        return {
            "status": described["EndpointStatus"],
            "endpoint": described["EndpointName"],
            "config": described.get("EndpointConfigName"),
            "variants": [v.get("VariantName") for v in described.get("ProductionVariants", [])],
            "capture": bool((described.get("DataCaptureConfig") or {}).get("EnableCapture")),
            "failure": described.get("FailureReason"),
        }

    def delete(self, tenant: Tenant, name: str) -> None:
        from botocore.exceptions import ClientError

        endpoint = tenant.resource(name)
        try:
            config = self.sm.describe_endpoint(EndpointName=endpoint).get("EndpointConfigName")
            self.sm.delete_endpoint(EndpointName=endpoint)
            if config:
                self.sm.delete_endpoint_config(EndpointConfigName=config)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ValidationException":
                raise


# ----- Prompts: Bedrock Prompt Management -----------------------------------------------


class BedrockPrompts:
    """A prompt per tenant and name, a numbered version per registration, the stage as a tag on
    the version's ARN, and the registry's sha256_12 on every version."""

    def __init__(self, agent: Any, config: AwsPlatformConfig) -> None:
        self.agent = agent
        self.cfg = config

    def register(
        self, tenant: Tenant, name: str, text: str, tags: Mapping[str, str]
    ) -> PromptVersion:
        sha = prompt_hash(text)
        prompt_name = _prompt_name(tenant, name)
        variant = {
            "name": "default",
            "templateType": "TEXT",
            "templateConfiguration": {"text": {"text": text}},
        }
        existing = self._find(prompt_name)
        all_tags = {
            **{k: str(v) for k, v in tags.items()},
            "nw:prompt": name,
            "nw:sha256_12": sha,
            "nw:tenant": tenant.name,
        }
        if existing:
            self.agent.update_prompt(
                promptIdentifier=existing,
                name=prompt_name,
                defaultVariant="default",
                variants=[variant],
                description=f"{name}@{sha}",
            )
            prompt_id = existing
        else:
            created = self.agent.create_prompt(
                name=prompt_name,
                description=f"{name}@{sha}",
                defaultVariant="default",
                variants=[variant],
                tags=all_tags,
            )
            prompt_id = created["id"]
        version = self.agent.create_prompt_version(
            promptIdentifier=prompt_id, description=sha, tags=all_tags
        )
        self.agent.tag_resource(
            resourceArn=version["arn"], tags={**all_tags, "nw:stage": Stage.CANDIDATE.value}
        )
        return PromptVersion(
            name=name,
            version=version["version"],
            text=text,
            sha256_12=sha,
            stage=Stage.CANDIDATE,
            tags=dict(tags),
        )

    def get(self, tenant: Tenant, name: str, version: str | None = None) -> PromptVersion:
        prompt_id = self._require(tenant, name)
        if version is None:
            version = self._live_version(prompt_id) or self._latest_version(prompt_id)
        fetched = self.agent.get_prompt(promptIdentifier=prompt_id, promptVersion=version)
        return self._to_version(name, fetched)

    def set_stage(self, tenant: Tenant, name: str, version: str, stage: Stage) -> PromptVersion:
        prompt_id = self._require(tenant, name)
        fetched = self.agent.get_prompt(promptIdentifier=prompt_id, promptVersion=version)
        self.agent.tag_resource(resourceArn=fetched["arn"], tags={"nw:stage": stage.value})
        if stage == Stage.LIVE:
            base_arn = fetched["arn"].rsplit(":", 1)[0]
            self.agent.tag_resource(resourceArn=base_arn, tags={"nw:live_version": version})
        return self._to_version(name, fetched, stage=stage)

    def versions(self, tenant: Tenant, name: str) -> Sequence[PromptVersion]:
        prompt_id = self._require(tenant, name)
        out: list[PromptVersion] = []
        for summary in self._version_summaries(prompt_id):
            if summary.get("version") == "DRAFT":
                continue
            out.append(self.get(tenant, name, summary["version"]))
        return out

    # helpers
    def _find(self, prompt_name: str) -> str | None:
        token: str | None = None
        while True:
            kw = {"nextToken": token} if token else {}
            page = self.agent.list_prompts(maxResults=100, **kw)
            for summary in page.get("promptSummaries", []):
                if summary["name"] == prompt_name:
                    return summary["id"]
            token = page.get("nextToken")
            if not token:
                return None

    def _require(self, tenant: Tenant, name: str) -> str:
        found = self._find(_prompt_name(tenant, name))
        if not found:
            raise KeyError(f"no prompt {_prompt_name(tenant, name)} in Bedrock Prompt Management")
        return found

    def _version_summaries(self, prompt_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kw = {"nextToken": token} if token else {}
            page = self.agent.list_prompts(promptIdentifier=prompt_id, maxResults=100, **kw)
            out.extend(page.get("promptSummaries", []))
            token = page.get("nextToken")
            if not token:
                return out

    def _latest_version(self, prompt_id: str) -> str:
        numbered = [
            s["version"]
            for s in self._version_summaries(prompt_id)
            if s.get("version", "DRAFT").isdigit()
        ]
        if not numbered:
            return "DRAFT"
        return max(numbered, key=int)

    def _live_version(self, prompt_id: str) -> str | None:
        for summary in self._version_summaries(prompt_id):
            if summary.get("version") == "DRAFT":
                tags = self.agent.list_tags_for_resource(resourceArn=summary["arn"]).get("tags", {})
                return tags.get("nw:live_version")
        return None

    def _to_version(
        self, name: str, fetched: Mapping[str, Any], *, stage: Stage | None = None
    ) -> PromptVersion:
        text = fetched["variants"][0]["templateConfiguration"]["text"]["text"]
        tags = self.agent.list_tags_for_resource(resourceArn=fetched["arn"]).get("tags", {})
        stage = stage or Stage(tags.get("nw:stage", Stage.CANDIDATE.value))
        return PromptVersion(
            name=name,
            version=fetched["version"],
            text=text,
            sha256_12=tags.get("nw:sha256_12") or prompt_hash(text),
            stage=stage,
            tags={k: v for k, v in tags.items() if not k.startswith("nw:")},
        )


def _prompt_name(tenant: Tenant, name: str) -> str:
    return tenant.resource(name.replace(".", "-").replace("_", "-"))


# ----- Vectors: Bedrock Knowledge Bases on S3 Vectors ------------------------------------


class BedrockKnowledgeBase:
    """Upsert writes the documents (and their `.metadata.json` sidecars) under the tenant's
    data-source prefix and starts an ingestion job; search is the Retrieve API. Vectors passed
    in are ignored: the knowledge base embeds with its own model."""

    def __init__(
        self,
        agent: Any,
        runtime: Any,
        s3: Any,
        config: AwsPlatformConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 10.0,
    ) -> None:
        self.agent = agent
        self.runtime = runtime
        self.s3 = s3
        self.cfg = config
        self.sleep = sleep
        self.poll_s = poll_s

    def upsert(
        self,
        tenant: Tenant,
        collection: str,
        ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]] | None,
        metadata: Sequence[Mapping[str, Any]],
    ) -> int:
        prefix = self._prefix(tenant, collection)
        for id_, text, meta in zip(ids, texts, metadata, strict=True):
            self.s3.put_object(
                Bucket=self.cfg.data_bucket, Key=f"{prefix}{id_}.txt", Body=text.encode()
            )
            self.s3.put_object(
                Bucket=self.cfg.data_bucket,
                Key=f"{prefix}{id_}.txt.metadata.json",
                Body=json.dumps({"metadataAttributes": {"chunk_id": id_, **dict(meta)}}).encode(),
            )
        self._sync(tenant)
        return len(ids)

    def search(
        self,
        tenant: Tenant,
        collection: str,
        query: str,
        k: int = 8,
        vector: Sequence[float] | None = None,
    ) -> Sequence[Hit]:
        response = self.runtime.retrieve(
            knowledgeBaseId=self._kb(tenant),
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}},
        )
        hits: list[Hit] = []
        for result in response.get("retrievalResults", []):
            meta = dict(result.get("metadata") or {})
            source = (result.get("location") or {}).get("s3Location", {}).get("uri", "")
            hit_id = str(
                meta.get("chunk_id") or source.rsplit("/", 1)[-1].removesuffix(".txt") or len(hits)
            )
            hits.append(
                Hit(
                    id=hit_id,
                    text=result.get("content", {}).get("text", ""),
                    score=float(result.get("score", 0.0)),
                    metadata={**meta, "source": source},
                )
            )
        return hits

    def count(self, tenant: Tenant, collection: str) -> int:
        prefix = self._prefix(tenant, collection)
        total = 0
        token: str | None = None
        while True:
            kw = {"ContinuationToken": token} if token else {}
            page = self.s3.list_objects_v2(Bucket=self.cfg.data_bucket, Prefix=prefix, **kw)
            total += sum(1 for o in page.get("Contents", []) if o["Key"].endswith(".txt"))
            token = page.get("NextContinuationToken")
            if not token:
                return total

    def drop(self, tenant: Tenant, collection: str) -> None:
        prefix = self._prefix(tenant, collection)
        token: str | None = None
        while True:
            kw = {"ContinuationToken": token} if token else {}
            page = self.s3.list_objects_v2(Bucket=self.cfg.data_bucket, Prefix=prefix, **kw)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(
                    Bucket=self.cfg.data_bucket, Delete={"Objects": keys, "Quiet": True}
                )
            token = page.get("NextContinuationToken")
            if not token:
                break
        self._sync(tenant)

    def _prefix(self, tenant: Tenant, collection: str) -> str:
        if collection != "policies":
            raise ValueError(
                "the knowledge base data source is tenants/<tenant>/policies/; "
                "the only collection is 'policies'"
            )
        return f"tenants/{tenant.name}/{collection}/"

    def _kb(self, tenant: Tenant) -> str:
        try:
            return self.cfg.knowledge_bases[tenant.name]
        except KeyError as exc:
            raise KeyError(
                f"no knowledge base for tenant {tenant.name}: "
                "set NW_AWS_KNOWLEDGE_BASES or deploy the stack"
            ) from exc

    def _data_source(self, tenant: Tenant, kb: str) -> str:
        if tenant.name in self.cfg.data_sources:
            return self.cfg.data_sources[tenant.name]
        page = self.agent.list_data_sources(knowledgeBaseId=kb, maxResults=10)
        sources = page.get("dataSourceSummaries", [])
        if not sources:
            raise KeyError(f"knowledge base {kb} has no data source")
        self.cfg.data_sources[tenant.name] = sources[0]["dataSourceId"]
        return sources[0]["dataSourceId"]

    def _sync(self, tenant: Tenant) -> None:
        kb = self._kb(tenant)
        ds = self._data_source(tenant, kb)
        job = self.agent.start_ingestion_job(
            knowledgeBaseId=kb, dataSourceId=ds, clientToken=str(uuid.uuid4())
        )["ingestionJob"]
        while job["status"] not in ("COMPLETE", "FAILED", "STOPPED"):
            self.sleep(self.poll_s)
            job = self.agent.get_ingestion_job(
                knowledgeBaseId=kb, dataSourceId=ds, ingestionJobId=job["ingestionJobId"]
            )["ingestionJob"]
        if job["status"] != "COMPLETE":
            raise RuntimeError(
                f"ingestion {job['ingestionJobId']} {job['status']}: {job.get('failureReasons')}"
            )


# ----- Agents: AgentCore Runtime and the Agent Registry ----------------------------------


class AgentCoreRuntime:
    """One runtime per tenant (`<environment>_<tenant>_resolver`) created or updated through the
    control plane, invoked through the data plane, and registered as an A2A agent card in the
    platform's Agent Registry (the record then waits for a curator's approval)."""

    def __init__(self, control: Any, data: Any, registry: Any, config: AwsPlatformConfig) -> None:
        self.control = control
        self.data = data
        self.registry = registry
        self.cfg = config
        self._arns: dict[str, tuple[str, str]] = {}  # runtime name -> (id, arn)

    def deploy(self, tenant: Tenant, image: str, env: Mapping[str, str], *, version: str) -> str:
        name = self.runtime_name(tenant)
        variables = {
            **dict(env),
            "NW_TRACK": "aws",
            "NW_ENVIRONMENT": self.cfg.environment,
            "NW_TENANT": tenant.name,
            "NW_AGENT_VERSION": version,
            "NW_APP": "nw.agent.agentcore:app",
            "NW_AGENT_ROLE": "resolver",
            "PORT": "8080",
        }
        found = self._lookup(name)
        artifact = {"containerConfiguration": {"containerUri": image}}
        if found:
            runtime_id, _ = found
            response = self.control.update_agent_runtime(
                agentRuntimeId=runtime_id,
                agentRuntimeArtifact=artifact,
                roleArn=self.cfg.runtime_role_arn,
                networkConfiguration={"networkMode": "PUBLIC"},
                protocolConfiguration={"serverProtocol": "HTTP"},
                environmentVariables=variables,
                description=f"{tenant.name} resolver, agent version {version}",
            )
        else:
            response = self.control.create_agent_runtime(
                agentRuntimeName=name,
                agentRuntimeArtifact=artifact,
                roleArn=self.cfg.runtime_role_arn,
                networkConfiguration={"networkMode": "PUBLIC"},
                protocolConfiguration={"serverProtocol": "HTTP"},
                environmentVariables=variables,
                description=f"{tenant.name} resolver, agent version {version}",
                clientToken=str(uuid.uuid4()),
            )
        self._arns[name] = (response["agentRuntimeId"], response["agentRuntimeArn"])
        return response["agentRuntimeArn"]

    def invoke(
        self, tenant: Tenant, payload: Mapping[str, Any], *, session_id: str | None = None
    ) -> Mapping[str, Any]:
        found = self._lookup(self.runtime_name(tenant))
        if not found:
            raise KeyError(f"runtime {self.runtime_name(tenant)} is not deployed")
        _, arn = found
        # AgentCore requires a session id of at least 33 characters.
        session = session_id or f"{tenant.name}-{str(uuid.uuid4())}"
        if len(session) < 33:
            session = f"{session}-{str(uuid.uuid4())}"
        response = self.data.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode(),
        )
        raw = response["response"].read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"text": raw.decode("utf-8", "replace")}
        if isinstance(body, dict):
            body.setdefault("session_id", response.get("runtimeSessionId", session))
            return body
        return {"result": body, "session_id": response.get("runtimeSessionId", session)}

    def register(self, tenant: Tenant, card: Mapping[str, Any]) -> str:
        from botocore.exceptions import ClientError

        name = tenant.resource("resolver")
        version = str(card.get("version") or "1.0.0")
        descriptors = {"a2aAgentCard": {"data": json.dumps(dict(card)), "dataSchemaVersion": "0.3"}}
        try:
            created = self.registry.create_registry_record(
                registryId=self.cfg.registry_id,
                name=name,
                displayName=str(card.get("name") or name),
                description=str(card.get("description") or f"{tenant.name} resolver")[:4096],
                recordType="AGENT",
                descriptors=descriptors,
                recordVersion=version,
                clientToken=str(uuid.uuid4()),
            )
            arn = created["recordArn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConflictException":
                raise
            record_id = self._record_id(name)
            # UpdateRegistryRecord wraps every optional field in `optionalValue` (the SDK's
            # "set or clear" shape), unlike CreateRegistryRecord.
            card_update = {
                "data": {"optionalValue": descriptors["a2aAgentCard"]["data"]},
                "dataSchemaVersion": {"optionalValue": "0.3"},
            }
            updated = self.registry.update_registry_record(
                registryId=self.cfg.registry_id,
                recordId=record_id,
                descriptors={"optionalValue": {"a2aAgentCard": {"optionalValue": card_update}}},
                recordVersion=version,
            )
            arn = updated["recordArn"]
        record_id = arn.rsplit("/", 1)[-1]
        self.registry.submit_registry_record_for_approval(
            registryId=self.cfg.registry_id, recordId=record_id
        )
        return arn

    def status(self, tenant: Tenant) -> Mapping[str, Any]:
        found = self._lookup(self.runtime_name(tenant))
        if not found:
            return {"status": "absent", "runtime": self.runtime_name(tenant)}
        runtime_id, arn = found
        described = self.control.get_agent_runtime(agentRuntimeId=runtime_id)
        return {
            "status": described["status"],
            "runtime": described["agentRuntimeName"],
            "arn": arn,
            "version": described.get("agentRuntimeVersion"),
            "agent_version": (described.get("environmentVariables") or {}).get("NW_AGENT_VERSION"),
            "failure": described.get("failureReason"),
        }

    def runtime_name(self, tenant: Tenant) -> str:
        return f"{self.cfg.under()}_{tenant.name}_resolver"

    def _lookup(self, name: str) -> tuple[str, str] | None:
        if name in self._arns:
            return self._arns[name]
        token: str | None = None
        while True:
            kw = {"nextToken": token} if token else {}
            page = self.control.list_agent_runtimes(maxResults=100, **kw)
            for summary in page.get("agentRuntimes", []):
                if summary.get("agentRuntimeName") == name:
                    self._arns[name] = (summary["agentRuntimeId"], summary["agentRuntimeArn"])
                    return self._arns[name]
            token = page.get("nextToken")
            if not token:
                return None

    def _record_id(self, name: str) -> str:
        token: str | None = None
        while True:
            kw = {"nextToken": token} if token else {}
            page = self.registry.list_registry_records(
                registryId=self.cfg.registry_id, maxResults=100, **kw
            )
            for record in page.get("registryRecords", []):
                if record.get("name") == name:
                    return record["recordId"]
            token = page.get("nextToken")
            if not token:
                raise KeyError(f"registry record {name} not found")


# ----- wiring -----------------------------------------------------------------------------


def build(settings: Settings, config: AwsPlatformConfig | None = None) -> Platform:
    import boto3

    cfg = config or AwsPlatformConfig.from_settings(settings)
    session = boto3.session.Session(region_name=cfg.region, profile_name=settings.aws_profile)
    sagemaker = session.client("sagemaker")
    s3 = session.client("s3")
    registry = SageMakerRegistry(sagemaker, s3, cfg)
    return Platform(
        track=Track.AWS,
        registry=registry,
        pipelines=SageMakerPipelines(sagemaker, session.client("logs"), cfg),
        endpoints=SageMakerEndpoints(sagemaker, session.client("sagemaker-runtime"), registry, cfg),
        prompts=BedrockPrompts(session.client("bedrock-agent"), cfg),
        vectors=BedrockKnowledgeBase(
            session.client("bedrock-agent"), session.client("bedrock-agent-runtime"), s3, cfg
        ),
        agents=AgentCoreRuntime(
            session.client("bedrock-agentcore-control"),
            session.client("bedrock-agentcore"),
            session.client("agent-registry-control"),
            cfg,
        ),
        gateway_url=cfg.gateway_url,
    )


# ----- CLI: the prompt catalog the stack reads --------------------------------------------


def prompts_catalog() -> list[dict[str, str]]:
    """Every registered prompt with its hash and text, sorted by name."""
    from nw.llm import prompts

    prompts.load_known()
    return [{"name": p.name, "sha256_12": p.hash, "text": p.text} for p in prompts.registered()]


def write_catalog(path: Path = CATALOG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prompts_catalog(), indent=2, ensure_ascii=False) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "describe"
    if command == "prompts-catalog":
        out = Path(argv[1]) if len(argv) > 1 else CATALOG
        path = write_catalog(out)
        print(f"wrote {path} ({len(json.loads(path.read_text()))} prompts)")
        return 0
    if command == "pipeline-upsert":
        from nw.config import settings as load_settings
        from nw.platform.base import tenant_from_env

        names = argv[1:] or ["triage", "semantic"]
        cfg = load_settings()
        runner = build(cfg).pipelines
        tenant = tenant_from_env(cfg)
        for name in names:
            print(f"{name}: {runner.upsert(tenant, name)}")
        return 0
    if command == "describe":
        settings = Settings(_env_file=None)
        cfg = AwsPlatformConfig.from_settings(settings)
        for k, v in cfg.__dict__.items():
            print(f"{k:<18} {v}")
        return 0
    print(
        "usage: python -m nw.platform.aws prompts-catalog [path] | describe"
        " | pipeline-upsert [triage|semantic ...]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
