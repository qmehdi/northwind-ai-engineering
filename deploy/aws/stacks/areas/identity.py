"""Identity and observability: the Cognito user pool for the web entry, the CloudTrail trail,
the platform dashboard, the alerts topic and the budget.

The trail has its own KMS key whose policy is the one the CloudTrail guide requires
(create-kms-key-policy-for-cloudtrail, fetched 2026-09-30): `kms:GenerateDataKey*` for
`cloudtrail.amazonaws.com` conditioned on `aws:SourceArn` (this trail) and the
`aws:cloudtrail:arn` encryption context, `kms:DescribeKey` for the trail, and `kms:Decrypt` for
the account's principals on CloudTrail ciphertext only. Without those statements CreateTrail
fails with InsufficientEncryptionPolicyException and the stack rolls back; synth and cdk-nag do
not catch it, `tests/test_synth.py` does.

Bedrock model invocation logging is on, metadata only (model, identity, token counts; no
prompt or completion text, which carries customer PII), to a log group kept 400 days. It is an
account and region setting with no CloudFormation resource, so a custom resource calls
`PutModelInvocationLoggingConfiguration` and removes it on delete.

At 100 percent of the monthly budget a Budgets action attaches a deny policy to every tenant's
learner and execution roles (SageMaker create and start, Bedrock invoke, AgentCore create and
invoke): a budget that only alerts does not stop a learner's forgotten endpoint.

Every alarm in every area pages the one topic. The dashboard shows the platform as the
figures draw it: the live endpoints, the policy API and its Lambda, the model gateway and its
database, Bedrock tokens per inference profile (one per tenant and role, so the panel is the
cost line per learner), and the exported `Northwind` series with the drift alerts.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks.areas.data import RETENTION
from stacks.common import LIVE, PROJECTS, alerts_topic, invoke_id, monthly_budget


class Identity(Construct):
    """Cognito and the alerts topic; created first because the other areas need both."""

    def __init__(
        self, scope: Construct, id: str, *, prefix: str, env_name: str, alert_email: str | None
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.topic: sns.Topic = alerts_topic(self, alert_email, stage=env_name)
        self.user_pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name=f"{prefix}-web",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        self.client = self.user_pool.add_client(
            "Web",
            user_pool_client_name=f"{prefix}-web",
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            generate_secret=False,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            prevent_user_existence_errors=True,
        )
        self.domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{prefix}-{stack.account}"),
        )
        self.domain_url = self.domain.base_url()
        CfnOutput(self, "OutUserPoolId", value=self.user_pool.user_pool_id).override_logical_id(
            "UserPoolId"
        )
        CfnOutput(
            self, "OutUserPoolClientId", value=self.client.user_pool_client_id
        ).override_logical_id("UserPoolClientId")
        CfnOutput(self, "OutCognitoDomain", value=self.domain_url).override_logical_id(
            "CognitoDomain"
        )
        CfnOutput(self, "OutAlertsTopicArn", value=self.topic.topic_arn).override_logical_id(
            "AlertsTopicArn"
        )


class Observability(Construct):
    """The trail, the dashboard and the budget; created last because it reads every area."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        env_name: str,
        key: kms.IKey,
        logs_bucket: s3.IBucket,
        topic: sns.ITopic,
        budget_usd: float,
        alert_email: str | None,
        tenants: list[str],
        policy_fn,
        endpoint_alarms: dict[str, list[cw.Alarm]],
        gateway,
        agents,
        profiles,
        stop_roles: list[iam.IRole] | None = None,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        trail_name = f"{prefix}-platform"
        trail_arn = f"arn:aws:cloudtrail:{stack.region}:{stack.account}:trail/{trail_name}"
        self.trail_key = kms.Key(
            self,
            "TrailKey",
            alias=f"alias/{prefix}-trail",
            description=f"{prefix} CloudTrail log and digest files",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.trail_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailEncryptLogs",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceArn": trail_arn},
                    "StringLike": {
                        "kms:EncryptionContext:aws:cloudtrail:arn": f"arn:aws:cloudtrail:*:{stack.account}:trail/*"
                    },
                },
            )
        )
        self.trail_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailDescribeKey",
                principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
                actions=["kms:DescribeKey"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceArn": trail_arn}},
            )
        )
        self.trail_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="EnableCloudTrailLogDecryptPermissions",
                principals=[iam.AccountRootPrincipal()],
                actions=["kms:Decrypt", "kms:ReEncryptFrom"],
                resources=["*"],
                conditions={
                    "StringEquals": {"kms:CallerAccount": stack.account},
                    "Null": {"kms:EncryptionContext:aws:cloudtrail:arn": "false"},
                },
            )
        )
        trail_bucket = s3.Bucket(
            self,
            "TrailBucket",
            bucket_name=f"{prefix}-trail-{stack.account}-{stack.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.trail_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="trail/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(id="audit", expiration=Duration.days(RETENTION["audit"]))
            ],
        )
        self.trail = cloudtrail.Trail(
            self,
            "Trail",
            trail_name=trail_name,
            bucket=trail_bucket,
            encryption_key=self.trail_key,
            send_to_cloud_watch_logs=True,
            cloud_watch_logs_retention=logs.RetentionDays.THIRTEEN_MONTHS,
            management_events=cloudtrail.ReadWriteType.ALL,
            is_multi_region_trail=False,
            include_global_service_events=True,
            enable_file_validation=True,
        )
        self.trail.node.add_dependency(self.trail_key)
        self._invocation_logging(prefix)

        alb_5xx = cw.Alarm(
            self,
            "AlarmGateway5xx",
            alarm_name=f"{prefix}-gateway-5xx",
            metric=gateway.alb.metrics.http_code_target(
                elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=10,
            evaluation_periods=2,
            alarm_description="model gateway: more than 10 server errors in 5 minutes, twice",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alb_5xx.add_alarm_action(cw_actions.SnsAction(topic))

        stage = env_name or "default"
        board = cw.Dashboard(self, "Dashboard", dashboard_name=f"{prefix}-platform")

        def exported(service: str, metric: str, statistic: str) -> cw.Metric:
            return cw.Metric(
                namespace="Northwind",
                metric_name=metric,
                dimensions_map={"Service": service, "Stage": stage},
                statistic=statistic,
                period=Duration.minutes(1),
                label=f"{service} {metric}",
            )

        board.add_widgets(
            cw.GraphWidget(
                title="live endpoints: invocations",
                left=[
                    cw.Metric(
                        namespace="AWS/SageMaker",
                        metric_name="Invocations",
                        dimensions_map={
                            "EndpointName": f"{prefix}-{LIVE}-{p}",
                            "VariantName": "AllTraffic",
                        },
                        statistic="Sum",
                        label=p,
                    )
                    for p in PROJECTS
                ],
            ),
            cw.GraphWidget(
                title="live endpoints: model latency p95 (microseconds)",
                left=[
                    cw.Metric(
                        namespace="AWS/SageMaker",
                        metric_name="ModelLatency",
                        dimensions_map={
                            "EndpointName": f"{prefix}-{LIVE}-{p}",
                            "VariantName": "AllTraffic",
                        },
                        statistic="p95",
                        label=p,
                    )
                    for p in PROJECTS
                ],
            ),
            cw.AlarmStatusWidget(
                title="endpoint and canary alarms",
                alarms=[a for alarms in endpoint_alarms.values() for a in alarms],
            ),
        )
        board.add_widgets(
            cw.GraphWidget(
                title="policy: invocations and errors (Lambda)",
                left=[
                    policy_fn.metric_invocations(statistic="Sum"),
                    policy_fn.metric_errors(statistic="Sum"),
                ],
            ),
            cw.GraphWidget(
                title="policy: duration p50 p95 (Lambda, includes init)",
                left=[
                    policy_fn.metric_duration(statistic="p50"),
                    policy_fn.metric_duration(statistic="p95"),
                ],
            ),
            cw.GraphWidget(
                title="policy: requests, errors, p95 (exported)",
                left=[exported("policy", "Requests", "Sum"), exported("policy", "Errors", "Sum")],
                right=[exported("policy", "LatencyP95Ms", "Maximum")],
            ),
        )
        board.add_widgets(
            cw.GraphWidget(
                title="model gateway: requests and 5xx (ALB)",
                left=[
                    gateway.alb.metrics.request_count(statistic="Sum"),
                    gateway.alb.metrics.http_code_target(
                        elbv2.HttpCodeTarget.TARGET_5XX_COUNT, statistic="Sum"
                    ),
                ],
            ),
            cw.GraphWidget(
                title="model gateway: task CPU and memory",
                left=[
                    gateway.service.service.metric_cpu_utilization(),
                    gateway.service.service.metric_memory_utilization(),
                ],
            ),
            cw.GraphWidget(
                title="gateway keys database: capacity units",
                left=[gateway.db.metric_serverless_database_capacity()],
            ),
        )

        def bedrock(metric: str, statistic: str = "Sum") -> list[cw.Metric]:
            return [
                cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name=metric,
                    statistic=statistic,
                    dimensions_map={"ModelId": profile.attr_inference_profile_id},
                    label=f"{owner} {role}",
                )
                for (owner, role), profile in profiles.items()
            ]

        board.add_widgets(
            cw.GraphWidget(
                title="Bedrock input tokens per tenant and role", left=bedrock("InputTokenCount")
            ),
            cw.GraphWidget(
                title="Bedrock output tokens per tenant and role", left=bedrock("OutputTokenCount")
            ),
            cw.GraphWidget(
                title="Bedrock invocations and throttles per model",
                left=[
                    cw.Metric(
                        namespace="AWS/Bedrock",
                        metric_name=m,
                        statistic="Sum",
                        dimensions_map={"ModelId": invoke_id(role, stack.region)},
                        label=f"{m} {role}",
                    )
                    for role in ("workhorse", "judge", "economy")
                    for m in ("Invocations", "InvocationThrottles")
                ],
            ),
        )
        board.add_widgets(
            cw.GraphWidget(
                title="model cost per minute, USD (exported)",
                left=[exported(s, "CostUsd", "Sum") for s in ("policy", "agent")],
            ),
            cw.GraphWidget(
                title="drift level per service (exported: 0 ok, 1 watch, 2 alert)",
                left=[
                    exported(s, "DriftLevel", "Maximum")
                    for s in ("triage", "semantic", "policy", "agent")
                ],
            ),
            cw.AlarmStatusWidget(
                title="drift and gateway alarms", alarms=[agents.drift_alarm, alb_5xx]
            ),
        )
        budget = monthly_budget(self, limit_usd=budget_usd, email=alert_email, stage=env_name)
        if stop_roles:
            self._budget_stop(prefix, budget, topic, stop_roles)
        CfnOutput(self, "OutDashboard", value=f"{prefix}-platform").override_logical_id("Dashboard")

    def _invocation_logging(self, prefix: str) -> None:
        """Bedrock model invocation logging, metadata only, through a custom resource."""
        stack = Stack.of(self)
        group = logs.LogGroup(
            self,
            "BedrockInvocations",
            log_group_name=f"/{prefix}/bedrock-invocations",
            retention=logs.RetentionDays.THIRTEEN_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            "BedrockLoggingRole",
            role_name=f"{prefix}-bedrock-logging",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{stack.region}:{stack.account}:*"
                    },
                },
            ),
            description="Bedrock writes model invocation log records to CloudWatch Logs",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{group.log_group_arn}:log-stream:*"],
            )
        )
        config = {
            "loggingConfig": {
                "cloudWatchConfig": {
                    "logGroupName": group.log_group_name,
                    "roleArn": role.role_arn,
                },
                "textDataDeliveryEnabled": False,
                "imageDataDeliveryEnabled": False,
                "embeddingDataDeliveryEnabled": False,
                "videoDataDeliveryEnabled": False,
            }
        }
        call = cr.AwsSdkCall(
            service="bedrock",
            action="PutModelInvocationLoggingConfiguration",
            parameters=config,
            physical_resource_id=cr.PhysicalResourceId.of(f"{prefix}-bedrock-invocation-logging"),
        )
        logging_cr = cr.AwsCustomResource(
            self,
            "InvocationLogging",
            on_create=call,
            on_update=call,
            on_delete=cr.AwsSdkCall(
                service="bedrock", action="DeleteModelInvocationLoggingConfiguration"
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "bedrock:PutModelInvocationLoggingConfiguration",
                            "bedrock:DeleteModelInvocationLoggingConfiguration",
                        ],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(actions=["iam:PassRole"], resources=[role.role_arn]),
                ]
            ),
            install_latest_aws_sdk=False,
        )
        logging_cr.node.add_dependency(role)
        logging_cr.node.add_dependency(group)

    def _budget_stop(self, prefix: str, budget, topic: sns.ITopic, roles: list[iam.IRole]) -> None:
        """At 100 percent actual spend, attach a deny policy to the tenants' roles."""
        stack = Stack.of(self)
        deny = iam.ManagedPolicy(
            self,
            "BudgetStop",
            managed_policy_name=f"{prefix}-budget-stop",
            description="Attached by the Budgets action at 100 percent: stops new spend",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    actions=[
                        "sagemaker:CreateTrainingJob",
                        "sagemaker:CreateProcessingJob",
                        "sagemaker:CreateTransformJob",
                        "sagemaker:CreateEndpoint",
                        "sagemaker:CreateEndpointConfig",
                        "sagemaker:UpdateEndpoint",
                        "sagemaker:StartPipelineExecution",
                        "sagemaker:CreateApp",
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:Converse",
                        "bedrock:ConverseStream",
                        "bedrock:StartIngestionJob",
                        "bedrock-agentcore:CreateAgentRuntime",
                        "bedrock-agentcore:UpdateAgentRuntime",
                        "bedrock-agentcore:InvokeAgentRuntime",
                    ],
                    resources=["*"],
                )
            ],
        )
        action_role = iam.Role(
            self,
            "BudgetActionRole",
            role_name=f"{prefix}-budget-action",
            assumed_by=iam.ServicePrincipal(
                "budgets.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
            ),
            description="AWS Budgets attaches the stop policy to the tenants' roles",
        )
        action_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                resources=[r.role_arn for r in roles],
                conditions={"ArnEquals": {"iam:PolicyARN": deny.managed_policy_arn}},
            )
        )
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="BudgetsPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
            )
        )
        action = budgets.CfnBudgetsAction(
            self,
            "BudgetStopAction",
            budget_name=budget.ref,
            notification_type="ACTUAL",
            action_type="APPLY_IAM_POLICY",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(
                type="PERCENTAGE", value=100
            ),
            execution_role_arn=action_role.role_arn,
            approval_model="AUTOMATIC",
            subscribers=[
                budgets.CfnBudgetsAction.SubscriberProperty(type="SNS", address=topic.topic_arn)
            ],
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=deny.managed_policy_arn, roles=[r.role_name for r in roles]
                )
            ),
        )
        action.node.add_dependency(action_role)
