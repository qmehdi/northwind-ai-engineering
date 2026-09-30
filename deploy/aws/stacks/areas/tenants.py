"""Tenants: the human identity a learner works with in cohort mode.

`northwind[-<env>]-<tenant>-learner` is the role a learner assumes for the whole documented
tenant workflow (`nw.platform.aws` from a laptop, Studio through a presigned URL), so no
instructor ever hands out administrator access. It is tagged `nw:tenant=<tenant>` and its trust
policy admits only principals of this account carrying the same tag (an IAM Identity Center
attribute for access control or an IAM user tag, README "Learner access"), so alice cannot
assume bob's role.

Scope, by attribute (ABAC) where the service names resources by id and by name prefix where it
names them by name:

- SageMaker: pipelines, jobs, models, endpoints and model packages under `northwind-<tenant>-*`,
  the promotion into the live package group (the approval deployer validates what it deploys),
  Studio through the tenant's own user profile, MLflow without deletes; compute only from
  `INSTANCE_TYPES`, no accelerators. PassRole only on the tenant's own execution, serving and
  AgentCore runtime roles, each to its one service.
- Data: `tenants/<tenant>/` read-write in both buckets, `data/` and `baselines/` read, MLflow
  artifacts written under `mlflow/tenants/<tenant>/`.
- Prompts: Bedrock prompts carrying `nw:tenant=<tenant>` (`aws:RequestTag` on create,
  `aws:ResourceTag` after), the tag itself unchangeable.
- Retrieval: `Retrieve` and ingestion on the tenant's knowledge base only, read on its S3 Vectors
  index.
- Models: the tenant's three application inference profiles only; the foundation models behind
  them only through those profiles (`bedrock:InferenceProfileArn`).
- Agents: AgentCore runtimes named `northwind_<tenant>_*` (create is authorised on `runtime/*`
  because the id is generated; the runtime can only run as the tenant's runtime role, which is
  the isolation), the tenant's memory, Agent Registry records (create, update, submit; never
  approve or delete), and the tenant's gateway and API key secrets.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_iam as iam
from constructs import Construct

from stacks.areas.tracking import SAGEMAKER_SERVICE, sagemaker_arn
from stacks.common import LIVE, PROJECTS, accelerator_guard, instance_type_guard, model_arns

TENANT_TAG = "nw:tenant"
PRINCIPAL_TENANT = "${aws:PrincipalTag/nw:tenant}"


class Tenants(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tenants: list[str],
        tracking,
        prompts,
        agents,
        gateway,
        serving,
        data,
    ) -> None:
        super().__init__(scope, id)
        self.roles: dict[str, iam.Role] = {}
        for tenant in tenants:
            self.roles[tenant] = self._learner(
                tenant,
                prefix=prefix,
                tracking=tracking,
                prompts=prompts,
                agents=agents,
                gateway=gateway,
                serving=serving,
                data=data,
            )
        CfnOutput(
            self,
            "OutLearnerRoles",
            value=",".join(f"{t}={r.role_arn}" for t, r in self.roles.items()),
        ).override_logical_id("LearnerRoles")

    def _learner(self, tenant, *, prefix, tracking, prompts, agents, gateway, serving, data):
        stack = Stack.of(self)
        region, account = stack.region, stack.account
        under = prefix.replace("-", "_")
        own = f"{prefix}-{tenant}-*"
        role = iam.Role(
            self,
            f"Learner{tenant.title()}",
            role_name=f"{prefix}-{tenant}-learner",
            assumed_by=iam.AccountPrincipal(account).with_conditions(
                {"StringEquals": {f"aws:PrincipalTag/{TENANT_TAG}": tenant}}
            ),
            max_session_duration=Duration.hours(8),
            description=f"{tenant}'s learner role: the documented tenant workflow and nothing of another tenant",
        )
        Tags.of(role).add(TENANT_TAG, tenant)

        def policy(name: str, statements: list[iam.PolicyStatement]) -> None:
            iam.ManagedPolicy(
                self,
                f"Learner{tenant.title()}{name}",
                managed_policy_name=f"{prefix}-{tenant}-learner-{name.lower()}",
                statements=statements,
                roles=[role],
            )

        profile_arns = [
            p.attr_inference_profile_arn for (o, _r), p in gateway.profiles.items() if o == tenant
        ]
        kb = prompts.knowledge_bases[tenant]
        kb_arn = f"arn:aws:bedrock:{region}:{account}:knowledge-base/{kb.attr_knowledge_base_id}"
        registry_arn = agents.registry.attr_registry_arn

        # ----- SageMaker, Studio, MLflow, schedules -----
        policy(
            "SageMaker",
            [
                iam.PolicyStatement(
                    sid="OwnSageMaker",
                    actions=[
                        "sagemaker:CreatePipeline",
                        "sagemaker:UpdatePipeline",
                        "sagemaker:DeletePipeline",
                        "sagemaker:DescribePipeline",
                        "sagemaker:DescribePipelineDefinitionForExecution",
                        "sagemaker:StartPipelineExecution",
                        "sagemaker:StopPipelineExecution",
                        "sagemaker:RetryPipelineExecution",
                        "sagemaker:DescribePipelineExecution",
                        "sagemaker:ListPipelineExecutionSteps",
                        "sagemaker:ListPipelineExecutions",
                        "sagemaker:DescribeTrainingJob",
                        "sagemaker:DescribeProcessingJob",
                        "sagemaker:StopTrainingJob",
                        "sagemaker:StopProcessingJob",
                        "sagemaker:CreateModel",
                        "sagemaker:DeleteModel",
                        "sagemaker:DescribeModel",
                        "sagemaker:CreateEndpointConfig",
                        "sagemaker:DeleteEndpointConfig",
                        "sagemaker:DescribeEndpointConfig",
                        "sagemaker:CreateEndpoint",
                        "sagemaker:UpdateEndpoint",
                        "sagemaker:DeleteEndpoint",
                        "sagemaker:DescribeEndpoint",
                        "sagemaker:InvokeEndpoint",
                        "sagemaker:CreateModelPackage",
                        "sagemaker:DescribeModelPackage",
                        "sagemaker:UpdateModelPackage",
                        "sagemaker:DescribeModelPackageGroup",
                        "sagemaker:ListModelPackages",
                        "sagemaker:AddTags",
                        "sagemaker:ListTags",
                    ],
                    resources=[
                        sagemaker_arn(self, kind, own)
                        for kind in (
                            "pipeline",
                            "pipeline-execution",
                            "training-job",
                            "processing-job",
                            "model",
                            "endpoint-config",
                            "endpoint",
                            "model-package",
                            "model-package-group",
                        )
                    ],
                ),
                # The promotion drill: re-register the approved package in the live group.
                iam.PolicyStatement(
                    sid="PromoteToLive",
                    actions=[
                        "sagemaker:CreateModelPackage",
                        "sagemaker:DescribeModelPackageGroup",
                        "sagemaker:DescribeModelPackage",
                        "sagemaker:ListModelPackages",
                        "sagemaker:AddTags",
                        "sagemaker:DescribeEndpoint",
                        "sagemaker:InvokeEndpoint",
                    ],
                    resources=[
                        sagemaker_arn(self, kind, f"{prefix}-{LIVE}-{p}{suffix}")
                        for p in PROJECTS
                        for kind, suffix in (
                            ("model-package-group", ""),
                            ("model-package", "/*"),
                            ("endpoint", ""),
                        )
                    ],
                ),
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
                        "sagemaker:ListDomains",
                        "sagemaker:ListUserProfiles",
                        "sagemaker:ListApps",
                        "sagemaker:ListSpaces",
                        "sagemaker:ListMlflowTrackingServers",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="Studio",
                    actions=[
                        "sagemaker:CreatePresignedDomainUrl",
                        "sagemaker:DescribeUserProfile",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:user-profile/{tracking.domain.attr_domain_id}/{prefix}-{tenant}"
                    ],
                ),
                iam.PolicyStatement(
                    sid="StudioDomain",
                    actions=["sagemaker:DescribeDomain"],
                    resources=[tracking.domain.attr_domain_arn],
                ),
            ],
        )
        policy(
            "Operate",
            [
                iam.PolicyStatement(
                    sid="Mlflow",
                    actions=[
                        "sagemaker-mlflow:*",
                        "sagemaker:CreatePresignedMlflowTrackingServerUrl",
                        "sagemaker:DescribeMlflowTrackingServer",
                    ],
                    resources=[
                        f"arn:aws:sagemaker:{region}:{account}:mlflow-tracking-server/{prefix}-mlflow"
                    ],
                ),
                iam.PolicyStatement(
                    sid="MlflowNoDeletes",
                    effect=iam.Effect.DENY,
                    actions=["sagemaker-mlflow:Delete*"],
                    resources=["*"],
                ),
                instance_type_guard(),
                accelerator_guard(),
                iam.PolicyStatement(
                    sid="PassOwnSageMakerRoles",
                    actions=["iam:PassRole"],
                    resources=[
                        tracking.tenant_roles[tenant].role_arn,
                        serving.serving_roles[tenant].role_arn,
                    ],
                    conditions={"StringEquals": {"iam:PassedToService": SAGEMAKER_SERVICE}},
                ),
                iam.PolicyStatement(
                    sid="OwnSchedules",
                    actions=["events:EnableRule", "events:DisableRule", "events:DescribeRule"],
                    resources=[
                        f"arn:aws:events:{region}:{account}:rule/{prefix}-{tenant}-retrain-*"
                    ],
                ),
                iam.PolicyStatement(
                    sid="ReadLogsAndMetrics",
                    actions=[
                        "logs:FilterLogEvents",
                        "logs:GetLogEvents",
                        "logs:DescribeLogStreams",
                    ],
                    resources=[
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/sagemaker/*",
                        f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{under}_{tenant}_*",
                    ],
                ),
                iam.PolicyStatement(
                    sid="ReadDashboards",
                    actions=[
                        "cloudwatch:GetMetricData",
                        "cloudwatch:ListMetrics",
                        "cloudwatch:GetDashboard",
                        "cloudwatch:DescribeAlarms",
                        "logs:DescribeLogGroups",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeVpcs",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # ----- data -----
        data_statements = [
            iam.PolicyStatement(
                sid="ListOwnPrefixes",
                actions=["s3:ListBucket"],
                resources=[data.data.bucket_arn, data.artifacts.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [
                            f"tenants/{tenant}/*",
                            f"tenants/{tenant}/",
                            "data/*",
                            "baselines/*",
                            "mlflow/*",
                        ]
                    }
                },
            ),
            iam.PolicyStatement(
                sid="ReadWriteOwnPrefix",
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[
                    data.data.arn_for_objects(f"tenants/{tenant}/*"),
                    data.artifacts.arn_for_objects(f"tenants/{tenant}/*"),
                    data.artifacts.arn_for_objects(f"mlflow/tenants/{tenant}/*"),
                ],
            ),
            iam.PolicyStatement(
                sid="ReadShared",
                actions=["s3:GetObject"],
                resources=[
                    data.data.arn_for_objects("data/*"),
                    data.artifacts.arn_for_objects("baselines/*"),
                    data.artifacts.arn_for_objects("mlflow/*"),
                ],
            ),
            iam.PolicyStatement(
                sid="KmsThroughS3",
                actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[data.key.key_arn],
                conditions={"StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"}},
            ),
            iam.PolicyStatement(
                sid="OwnSecrets",
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[
                    gateway.key_secret(tenant).secret_arn,
                    agents.tenant_api_keys[tenant].secret_arn,
                ],
            ),
            iam.PolicyStatement(
                sid="PromotedImages",
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{region}:{account}:parameter/{prefix}/images/*"],
            ),
            iam.PolicyStatement(
                sid="StackOutputs",
                actions=["cloudformation:DescribeStacks"],
                resources=[stack.stack_id],
            ),
        ]
        policy("Data", data_statements)

        # ----- prompts, retrieval, models -----
        policy(
            "GenAI",
            [
                iam.PolicyStatement(
                    sid="CreateOwnPrompts",
                    actions=["bedrock:CreatePrompt"],
                    resources=[f"arn:aws:bedrock:{region}:{account}:prompt/*"],
                    conditions={"StringEquals": {f"aws:RequestTag/{TENANT_TAG}": PRINCIPAL_TENANT}},
                ),
                iam.PolicyStatement(
                    sid="UseOwnPrompts",
                    actions=[
                        "bedrock:GetPrompt",
                        "bedrock:UpdatePrompt",
                        "bedrock:DeletePrompt",
                        "bedrock:CreatePromptVersion",
                        "bedrock:TagResource",
                    ],
                    resources=[f"arn:aws:bedrock:{region}:{account}:prompt/*"],
                    conditions={
                        "StringEquals": {f"aws:ResourceTag/{TENANT_TAG}": PRINCIPAL_TENANT},
                        "StringEqualsIfExists": {f"aws:RequestTag/{TENANT_TAG}": PRINCIPAL_TENANT},
                    },
                ),
                iam.PolicyStatement(
                    sid="ListPrompts",
                    actions=["bedrock:ListPrompts", "bedrock:ListTagsForResource"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="OwnKnowledgeBase",
                    actions=[
                        "bedrock:Retrieve",
                        "bedrock:GetKnowledgeBase",
                        "bedrock:ListDataSources",
                        "bedrock:GetDataSource",
                        "bedrock:StartIngestionJob",
                        "bedrock:GetIngestionJob",
                        "bedrock:ListIngestionJobs",
                    ],
                    resources=[kb_arn, f"{kb_arn}/*"],
                ),
                iam.PolicyStatement(
                    sid="OwnVectorIndex",
                    actions=[
                        "s3vectors:GetIndex",
                        "s3vectors:QueryVectors",
                        "s3vectors:GetVectors",
                        "s3vectors:ListVectors",
                    ],
                    resources=[prompts.indexes[tenant].attr_index_arn],
                ),
                iam.PolicyStatement(
                    sid="VectorBucket",
                    actions=["s3vectors:ListIndexes", "s3vectors:GetVectorBucket"],
                    resources=[prompts.vector_bucket.attr_vector_bucket_arn],
                ),
                iam.PolicyStatement(
                    sid="InvokeOwnProfiles",
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:Converse",
                        "bedrock:ConverseStream",
                        "bedrock:GetInferenceProfile",
                    ],
                    resources=profile_arns,
                ),
                iam.PolicyStatement(
                    sid="ModelsThroughOwnProfiles",
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=[a for a in model_arns(self) if "application-" not in a],
                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": profile_arns}},
                ),
                iam.PolicyStatement(
                    sid="Guardrail",
                    actions=["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"],
                    resources=[agents.guardrail.attr_guardrail_arn],
                ),
            ],
        )

        # ----- agents -----
        runtimes = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{under}_{tenant}_*"
        identities = (
            f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default"
        )
        policy(
            "Agents",
            [
                iam.PolicyStatement(
                    sid="CreateRuntime",
                    actions=["bedrock-agentcore:CreateAgentRuntime"],
                    resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*"],
                ),
                iam.PolicyStatement(
                    sid="OwnRuntimes",
                    actions=[
                        "bedrock-agentcore:UpdateAgentRuntime",
                        "bedrock-agentcore:GetAgentRuntime",
                        "bedrock-agentcore:DeleteAgentRuntime",
                        "bedrock-agentcore:InvokeAgentRuntime",
                        "bedrock-agentcore:ListAgentRuntimeVersions",
                        "bedrock-agentcore:ListAgentRuntimeEndpoints",
                        "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    ],
                    resources=[runtimes, f"{runtimes}/*"],
                ),
                iam.PolicyStatement(
                    sid="ListRuntimes",
                    actions=["bedrock-agentcore:ListAgentRuntimes"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="OwnWorkloadIdentity",
                    actions=[
                        "bedrock-agentcore:CreateWorkloadIdentity",
                        "bedrock-agentcore:GetWorkloadIdentity",
                        "bedrock-agentcore:UpdateWorkloadIdentity",
                        "bedrock-agentcore:DeleteWorkloadIdentity",
                    ],
                    resources=[identities, f"{identities}/workload-identity/{under}_{tenant}_*"],
                ),
                iam.PolicyStatement(
                    sid="PassOwnRuntimeRole",
                    actions=["iam:PassRole"],
                    resources=[agents.tenant_runtime_roles[tenant].role_arn],
                    conditions={
                        "StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}
                    },
                ),
                iam.PolicyStatement(
                    sid="OwnMemory",
                    actions=[
                        "bedrock-agentcore:GetMemory",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:GetEvent",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:ListMemoryRecords",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:GetMemoryRecord",
                        "bedrock-agentcore:DeleteMemoryRecord",
                        "bedrock-agentcore:DeleteEvent",
                    ],
                    resources=[agents.memories[tenant].attr_memory_arn],
                ),
                iam.PolicyStatement(
                    sid="RegistryRecords",
                    actions=[
                        "agent-registry:CreateRegistryRecord",
                        "agent-registry:UpdateRegistryRecord",
                        "agent-registry:GetRegistryRecord",
                        "agent-registry:ListRegistryRecords",
                        "agent-registry:SubmitRegistryRecordForApproval",
                        "agent-registry:GetRegistry",
                        "agent-registry:SearchDiscoverableRegistryRecords",
                        "agent-registry:ListDiscoverableRegistryRecords",
                        "agent-registry:GetDiscoverableRegistryRecord",
                    ],
                    resources=[registry_arn, f"{registry_arn}/record/*"],
                ),
                iam.PolicyStatement(
                    sid="NeverCurate",
                    effect=iam.Effect.DENY,
                    actions=[
                        "agent-registry:UpdateRegistryRecordStatus",
                        "agent-registry:DeleteRegistryRecord",
                    ],
                    resources=["*"],
                ),
            ],
        )
        return role
