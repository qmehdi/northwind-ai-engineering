"""Tracking and registry: the SageMaker domain, one user profile and execution role per tenant,
the managed MLflow tracking server, and one Model Package Group per project and tenant.

Isolation (ADR 0009) is the execution role: a tenant's role can create training and processing
jobs, pipelines, models and model packages only under its own name prefix
(`northwind-<tenant>-*`), read shared data under `data/` and write only under
`tenants/<tenant>/`. Approval status on the model package is the stage (`Stage` in
`nw/platform/base.py`): PendingManualApproval is a candidate, Approved is what the serving area
deploys, Rejected is retired.

Guards on every tenant execution role: compute only from `INSTANCE_TYPES` and no accelerators
(`sagemaker:InstanceTypes`, `sagemaker:AcceleratorTypes`), ECR pull only from this account's
platform repositories and the listed framework images, MLflow without deletes (the tracking
server has one IAM resource, so `sagemaker-mlflow:Delete*` is denied rather than scoped) and
MLflow artifacts written only under `mlflow/tenants/<tenant>/`.

Destroy: CloudFormation deletes the domain but keeps its home EFS file system and the two NFS
security groups SageMaker created, which then block the VPC's deletion. `DomainCleanup` is a
custom resource the domain depends on, so it is deleted after the domain and before the VPC,
and on delete it removes the file system, its mount targets and the security groups.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sm
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks.common import (
    PROJECTS,
    accelerator_guard,
    ecr_pull_statement,
    instance_type_guard,
)

FUNCTIONS = Path(__file__).resolve().parents[2] / "functions"

SAGEMAKER_SERVICE = "sagemaker.amazonaws.com"


def sagemaker_arn(scope: Construct, kind: str, name: str) -> str:
    stack = Stack.of(scope)
    return f"arn:aws:sagemaker:{stack.region}:{stack.account}:{kind}/{name}"


class TrackingRegistry(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tenants: list[str],
        vpc: ec2.IVpc,
        data: s3.IBucket,
        artifacts: s3.IBucket,
        key: kms.IKey,
    ) -> None:
        super().__init__(scope, id)
        self.prefix = prefix
        self.data, self.artifacts, self.key = data, artifacts, key

        # ----- MLflow: the tracking server and the role it reads and writes artifacts with -----
        mlflow_role = iam.Role(
            self,
            "MlflowRole",
            role_name=f"{prefix}-mlflow",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE),
            description="SageMaker managed MLflow: artifact store access",
        )
        artifacts.grant_read_write(mlflow_role, "mlflow/*")
        artifacts.grant_read(mlflow_role)
        key.grant_encrypt_decrypt(mlflow_role)
        self.mlflow = sm.CfnMlflowTrackingServer(
            self,
            "Mlflow",
            tracking_server_name=f"{prefix}-mlflow",
            artifact_store_uri=f"s3://{artifacts.bucket_name}/mlflow",
            role_arn=mlflow_role.role_arn,
            tracking_server_size="Small",
            automatic_model_registration=False,  # nw.platform registers with a card and a gate
            weekly_maintenance_window_start="Sun:03:00",
        )
        self.mlflow.node.add_dependency(mlflow_role)

        # ----- the domain: IAM auth, Studio through SageMaker's own VPC, EFS in ours -----
        self.platform_role = self._execution_role("Platform", "platform", live=True)
        self.domain = sm.CfnDomain(
            self,
            "Domain",
            domain_name=f"{prefix}-platform",
            auth_mode="IAM",
            vpc_id=vpc.vpc_id,
            subnet_ids=[s.subnet_id for s in vpc.public_subnets],
            app_network_access_type="PublicInternetOnly",
            kms_key_id=key.key_id,
            default_user_settings=sm.CfnDomain.UserSettingsProperty(
                execution_role=self.platform_role.role_arn,
                studio_web_portal="ENABLED",
                default_landing_uri="studio::",
            ),
        )

        self._domain_cleanup(prefix, vpc)

        # ----- one profile, one role and two package groups per tenant -----
        self.tenant_roles: dict[str, iam.Role] = {}
        self.profiles: dict[str, sm.CfnUserProfile] = {}
        self.groups: dict[tuple[str, str], sm.CfnModelPackageGroup] = {}
        for tenant in tenants:
            role = self._execution_role(f"Tenant{tenant.title()}", tenant)
            self.tenant_roles[tenant] = role
            self.profiles[tenant] = sm.CfnUserProfile(
                self,
                f"Profile{tenant.title()}",
                domain_id=self.domain.attr_domain_id,
                user_profile_name=f"{prefix}-{tenant}",
                user_settings=sm.CfnUserProfile.UserSettingsProperty(execution_role=role.role_arn),
            )
            for project in PROJECTS:
                group = sm.CfnModelPackageGroup(
                    self,
                    f"Group{tenant.title()}{project.title()}",
                    model_package_group_name=f"{prefix}-{tenant}-{project}",
                    model_package_group_description=(
                        f"{project} models trained by {tenant}; approval status is the stage"
                    ),
                )
                self.groups[(tenant, project)] = group
        CfnOutput(self, "OutDomainId", value=self.domain.attr_domain_id).override_logical_id(
            "DomainId"
        )
        CfnOutput(
            self, "OutMlflowArn", value=self.mlflow.attr_tracking_server_arn
        ).override_logical_id("MlflowArn")
        CfnOutput(self, "OutMlflowName", value=f"{prefix}-mlflow").override_logical_id("MlflowName")
        CfnOutput(self, "OutTenants", value=",".join(tenants)).override_logical_id("Tenants")

    def _execution_role(self, id: str, owner: str, *, live: bool = False) -> iam.Role:
        """A SageMaker execution role scoped to one owner's names. `owner` is a tenant, or
        `platform` for the domain default and the promoted (`live`) endpoints."""
        stack = Stack.of(self)
        own = f"{self.prefix}-{owner}-*"
        # Only ever one tenant's names: `northwind-alice-*` never matches `northwind-alicebob-*`
        # because the tenant is followed by `-`, and tenants cannot be environment words.
        names = [own, f"{self.prefix}-live-*"] if live else [own]
        role = iam.Role(
            self,
            f"Role{id}",
            role_name=f"{self.prefix}-{owner}-sagemaker",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE),
            description=f"SageMaker execution role for {owner}: jobs, pipelines and models under its prefix",
        )
        # Data: shared training data and the policy corpus read-only; the owner's prefix read-write.
        self.data.grant_read(role, "data/*")
        self.data.grant_read_write(role, f"tenants/{owner}/*")
        self.artifacts.grant_read_write(role, f"tenants/{owner}/*")
        self.artifacts.grant_read(role, "baselines/*")
        for bucket in (self.data, self.artifacts):
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=f"List{bucket.node.id}",
                    actions=["s3:ListBucket"],
                    resources=[bucket.bucket_arn],
                    conditions={
                        "StringLike": {
                            "s3:prefix": [
                                f"tenants/{owner}/*",
                                "data/*",
                                "baselines/*",
                                "mlflow/*",
                                "live/*" if live else f"tenants/{owner}/",
                            ]
                        }
                    },
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=f"Location{bucket.node.id}",
                    actions=["s3:GetBucketLocation"],
                    resources=[bucket.bucket_arn],
                )
            )
        self.key.grant_encrypt_decrypt(role)
        # Jobs, pipelines, models and packages, only under the owner's names.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="OwnResources",
                actions=[
                    "sagemaker:CreateTrainingJob",
                    "sagemaker:DescribeTrainingJob",
                    "sagemaker:StopTrainingJob",
                    "sagemaker:CreateProcessingJob",
                    "sagemaker:DescribeProcessingJob",
                    "sagemaker:StopProcessingJob",
                    "sagemaker:CreateTransformJob",
                    "sagemaker:DescribeTransformJob",
                    "sagemaker:CreatePipeline",
                    "sagemaker:UpdatePipeline",
                    "sagemaker:DeletePipeline",
                    "sagemaker:DescribePipeline",
                    "sagemaker:StartPipelineExecution",
                    "sagemaker:StopPipelineExecution",
                    "sagemaker:DescribePipelineExecution",
                    "sagemaker:ListPipelineExecutionSteps",
                    "sagemaker:ListPipelineExecutions",
                    "sagemaker:CreateModel",
                    "sagemaker:DeleteModel",
                    "sagemaker:DescribeModel",
                    "sagemaker:CreateEndpointConfig",
                    "sagemaker:DeleteEndpointConfig",
                    "sagemaker:CreateEndpoint",
                    "sagemaker:UpdateEndpoint",
                    "sagemaker:DeleteEndpoint",
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DescribeEndpointConfig",
                    "sagemaker:InvokeEndpoint",
                    "sagemaker:AddTags",
                    "sagemaker:ListTags",
                ],
                resources=[
                    sagemaker_arn(self, kind, n)
                    for kind in (
                        "training-job",
                        "processing-job",
                        "transform-job",
                        "pipeline",
                        "model",
                        "endpoint-config",
                        "endpoint",
                    )
                    for n in names
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="OwnModelPackages",
                # The pipeline's register step (a processing step running
                # nw.pipelines.steps.register) creates the version with its lineage tags.
                actions=[
                    "sagemaker:CreateModelPackage",
                    "sagemaker:AddTags",
                    "sagemaker:ListTags",
                    "sagemaker:DescribeModelPackage",
                    "sagemaker:UpdateModelPackage",
                    "sagemaker:DeleteModelPackage",
                    "sagemaker:DescribeModelPackageGroup",
                    "sagemaker:ListModelPackages",
                ],
                resources=[sagemaker_arn(self, "model-package", n) for n in names]
                + [sagemaker_arn(self, "model-package-group", n) for n in names],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListsAreAccountWide",
                actions=[
                    "sagemaker:ListModelPackageGroups",
                    "sagemaker:ListModelPackages",  # no resource-level scope: the champion lookup lists a group
                    "sagemaker:ListPipelines",
                    "sagemaker:ListTrainingJobs",
                    "sagemaker:ListProcessingJobs",
                    "sagemaker:ListEndpoints",
                    "sagemaker:ListModels",
                    "sagemaker:ListMonitoringSchedules",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Mlflow",
                actions=["sagemaker-mlflow:*"],
                resources=[
                    f"arn:aws:sagemaker:{stack.region}:{stack.account}:mlflow-tracking-server/{self.prefix}-mlflow"
                ],
            )
        )
        if not live:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="MlflowNoDeletes",
                    effect=iam.Effect.DENY,
                    actions=["sagemaker-mlflow:Delete*"],
                    resources=["*"],
                )
            )
        # MLflow artifacts: every run readable, writes only under the owner's prefix.
        self.artifacts.grant_read(role, "mlflow/*")
        self.artifacts.grant_put(role, f"mlflow/tenants/{owner}/*")
        role.add_to_policy(
            iam.PolicyStatement(
                sid="MlflowPresignedUrl",
                actions=["sagemaker:CreatePresignedMlflowTrackingServerUrl"],
                resources=[
                    f"arn:aws:sagemaker:{stack.region}:{stack.account}:mlflow-tracking-server/{self.prefix}-mlflow"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="JobLogsAndMetrics",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/sagemaker/*"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Metrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringLike": {"cloudwatch:namespace": ["/aws/sagemaker/*", "Northwind"]}
                },
            )
        )
        role.add_to_policy(ecr_pull_statement(self, self.prefix))
        role.add_to_policy(instance_type_guard())
        role.add_to_policy(accelerator_guard())
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrToken", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            )
        )
        # Pipelines and jobs pass the role to SageMaker again (the pipeline definition names it).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassSelf",
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{stack.account}:role/{self.prefix}-{owner}-sagemaker"],
                conditions={"StringEquals": {"iam:PassedToService": SAGEMAKER_SERVICE}},
            )
        )
        return role

    def _domain_cleanup(self, prefix: str, vpc: ec2.IVpc) -> None:
        """Delete the domain's home EFS and NFS security groups after the domain is deleted."""
        stack = Stack.of(self)
        fn = lam.Function(
            self,
            "DomainCleanupFn",
            function_name=f"{prefix}-domain-cleanup",
            runtime=lam.Runtime.PYTHON_3_12,
            architecture=lam.Architecture.ARM_64,
            handler="handler.on_event",
            code=lam.Code.from_asset(str(FUNCTIONS / "domain_cleanup")),
            timeout=Duration.minutes(14),
            memory_size=256,
            description="On stack delete: the SageMaker domain's home EFS and NFS security groups",
            log_group=logs.LogGroup(
                self,
                "DomainCleanupLogs",
                log_group_name=f"/aws/lambda/{prefix}-domain-cleanup",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="Describe",
                actions=[
                    "elasticfilesystem:DescribeFileSystems",
                    "elasticfilesystem:DescribeMountTargets",
                    "elasticfilesystem:DescribeMountTargetSecurityGroups",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeNetworkInterfaces",
                ],
                resources=["*"],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="DeleteDomainEfs",
                actions=[
                    "elasticfilesystem:DeleteMountTarget",
                    "elasticfilesystem:DeleteFileSystem",
                ],
                resources=[
                    f"arn:aws:elasticfilesystem:{stack.region}:{stack.account}:file-system/*"
                ],
                conditions={
                    "StringLike": {
                        "aws:ResourceTag/ManagedByAmazonSageMakerResource": f"arn:aws:sagemaker:{stack.region}:{stack.account}:domain/*"
                    }
                },
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="DeleteNfsGroups",
                actions=[
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupEgress",
                    "ec2:DeleteSecurityGroup",
                ],
                resources=[f"arn:aws:ec2:{stack.region}:{stack.account}:security-group/*"],
                conditions={
                    "ArnEquals": {
                        "ec2:Vpc": f"arn:aws:ec2:{stack.region}:{stack.account}:vpc/{vpc.vpc_id}"
                    }
                },
            )
        )
        provider = cr.Provider(self, "DomainCleanupProvider", on_event_handler=fn)
        cleanup = CustomResource(
            self,
            "DomainCleanup",
            service_token=provider.service_token,
            properties={"VpcId": vpc.vpc_id},
        )
        # Deleted after the domain (the domain depends on it), before the VPC (it references it).
        self.domain.node.add_dependency(cleanup)
