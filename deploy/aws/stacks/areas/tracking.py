"""Tracking and registry: the SageMaker domain, one user profile and execution role per tenant,
the managed MLflow tracking server, and one Model Package Group per project and tenant.

Isolation (ADR 0009) is the execution role: a tenant's role can create training and processing
jobs, pipelines, models and model packages only under its own name prefix
(`northwind-<tenant>-*`), read shared data under `data/` and write only under
`tenants/<tenant>/`. Approval status on the model package is the stage (`Stage` in
`nw/platform/base.py`): PendingManualApproval is a candidate, Approved is what the serving area
deploys, Rejected is retired.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sm
from constructs import Construct

from stacks.common import PROJECTS

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

        # ----- one profile, one role and two package groups per tenant -----
        self.tenant_roles: dict[str, iam.Role] = {}
        self.groups: dict[tuple[str, str], sm.CfnModelPackageGroup] = {}
        for tenant in tenants:
            role = self._execution_role(f"Tenant{tenant.title()}", tenant)
            self.tenant_roles[tenant] = role
            sm.CfnUserProfile(
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
                    actions=["s3:ListBucket", "s3:GetBucketLocation"],
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
                actions=[
                    "sagemaker:CreateModelPackage",
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
                    "StringEquals": {"cloudwatch:namespace": ["/aws/sagemaker/*", "Northwind"]}
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPull",
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[f"arn:aws:ecr:{stack.region}:*:repository/*"],
            )
        )
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
