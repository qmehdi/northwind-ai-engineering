"""Serving: the approval-driven deployer, the live endpoints' alarms and monitor, and the
policy service as the inference Lambda behind an HTTP API (the LLMOps reference's
`web UI -> API Gateway -> inference Lambda -> RAG store` path).

- `DeployOnApproval` (functions/deploy_on_approval) runs on every `Approved` model package of
  this platform: Serverless Inference for a tenant, the real-time endpoint with a canary,
  autoscaling, data capture and a Model Monitor schedule for `live`.
- Alarms on the live endpoints (`AWS/SageMaker` Invocation5XXErrors and ModelLatency p95, and
  the Model Monitor `feature_baseline_drift_<feature>` metric in
  `aws/sagemaker/Endpoints/data-metrics`) page the topic and are the canary's rollback triggers.
- The policy Lambda keeps the service-layer contract (`x-api-key`, probes open, EMF metrics,
  X-Ray spans) and gets the platform's settings: the live knowledge base, the gateway URL and
  the gateway key secret, and `NW_OPS_STORE` (the live prefix of the ops bucket, where
  `/feedback` verdicts land; the role reaches that prefix only). It publishes a version, the alias `live` fronts it, CodeDeploy shifts
  the alias (10 percent for 15 minutes) and rolls back on its errors or p95 alarm. The HTTP API
  authorises with the Cognito user pool; `/healthz` and `/readyz` stay open for probes.

Isolation of the deployer (the audit's cross-tenant finding): one serving role per owner
(`northwind-<owner>-serving`) that reads only that owner's artifacts; the deployer validates
every container of an approved package against an allow-list of image prefixes (this account's
platform repositories and the listed framework images) and its data URL against the owner's
prefix before it creates anything, and passes only the owner's role. A live promotion copies
the tenant's `model.tar.gz` and its Model Monitor baseline into `live/` and `monitoring/`
(platform-owned prefixes no tenant can write) and serves the copy with the live role. The
approval rule matches the exact package group names of this environment, so a second
environment's approvals never reach this deployer.

Model Monitor: a data quality job definition (the analyzer image, the record preprocessor that
turns a captured ticket into `text_length`, CloudWatch metrics on) and an hourly schedule. The
drift alarm treats missing data as not breaching, because it is a canary rollback trigger; a
second alarm, `-monitor-silent`, fires when the monitor has published nothing for three hours.

Quality canary alarms (`QUALITY_ALARMS`): the policy Lambda's CodeDeploy group and the live
endpoints' blue/green rollback also watch quality, not only errors and latency: `QualityLevel`
at 2 rolls back; `ShadowAgreement`, `P0ShareRatio`, `RefusalRatio` and `JudgeScore` page with
the signal that moved. The agent's alarms page only (AgentCore has no canary). A `quality_alert`
metric filter per log group counts the monitors' alert lines. Until a metric exists
an alarm sits in INSUFFICIENT_DATA and never blocks a deploy.

Image ownership: the policy Lambda's image URI comes from the SSM parameter
`/<environment>/images/policy`. While it says `asset` the stack uses the image it built; the
delivery pipeline writes the promoted digest there after a successful deploy, so the next
`cdk deploy` keeps the pipeline's image instead of rolling it back.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnCondition, CfnOutput, CfnParameter, Duration, Fn, RemovalPolicy, Size, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_codedeploy as cd
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as secrets
from aws_cdk import aws_sns as sns
from constructs import Construct

from stacks.areas.tracking import SAGEMAKER_SERVICE, sagemaker_arn
from stacks.common import (
    IMAGE_EXCLUDE,
    LIVE,
    PROJECTS,
    REPO_ROOT,
    SERVICES,
    accelerator_guard,
    allowed_image_prefixes,
    ecr_pull_statement,
    git_sha,
    image_parameter,
    instance_type_guard,
)

FUNCTIONS = Path(__file__).resolve().parents[2] / "functions"
# The Model Monitor analyzer image lives in an AWS account per region
# (sagemaker-python-sdk image_uri_config/model-monitor.json, read 2026-09-29).
MONITOR_ACCOUNTS = {
    "us-east-1": "156813124566",
    "us-east-2": "777275614652",
    "us-west-2": "159807026194",
    "eu-west-1": "468650794304",
    "eu-west-2": "749857270468",
    "eu-central-1": "048819808253",
    "ap-southeast-1": "245545462676",
    "ap-south-1": "126357580389",
    "ap-northeast-1": "574779866223",
    "ca-central-1": "536280801234",
}
# The feature the Model Monitor drift alarm watches. The baseline the pipeline writes names the
# captured request's fields; `text_length` is the one both projects' services already track for
# drift (`nw/triage/service.py`), so the managed monitor and the service agree on the signal.
MONITOR_FEATURE = "text_length"
DEPLOYMENT_CONFIG = cd.LambdaDeploymentConfig.CANARY_10_PERCENT_15_MINUTES
PREPROCESSOR = REPO_ROOT / "nw" / "serving" / "sagemaker" / "preprocessor.py"
PREPROCESSOR_KEY = "monitoring/code/preprocessor.py"
# Quality canary alarms on the signals the services export (`nw/metrics_export.py`, bars in
# `deploy/SLO.md` and `nw/quality.py`), namespace `Northwind`, dimensions Service and Stage, the
# same as every exported series: (service, name, metric, comparison, threshold, rollback).
# `QualityLevel` at 2 is the monitor's own verdict, which already holds the minimum samples (200
# requests, 20 judged runs) and the confidence interval, so it is the one alarm a canary rolls back
# on; the per-signal alarms page with the value that moved and never roll anything back. The
# predicted P0 share and the refusal rate are reported, not alarmed: the bars are on their ratios.
COMPARISONS = {
    "GreaterThanOrEqualToThreshold": cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    "LessThanOrEqualToThreshold": cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
    "LessThanThreshold": cw.ComparisonOperator.LESS_THAN_THRESHOLD,
}
_LEVEL = ("level", "QualityLevel", "GreaterThanOrEqualToThreshold", 2, True)
_CLASSIFIER = [
    _LEVEL,
    ("shadow", "ShadowAgreement", "LessThanThreshold", 0.9, False),
    ("p0-high", "P0ShareRatio", "GreaterThanOrEqualToThreshold", 2, False),
    ("p0-low", "P0ShareRatio", "LessThanOrEqualToThreshold", 0.5, False),
]
QUALITY_ALARMS = {
    "triage": _CLASSIFIER,
    "semantic": _CLASSIFIER,
    "policy": [_LEVEL, ("refusal", "RefusalRatio", "GreaterThanOrEqualToThreshold", 2, False)],
    "agent": [_LEVEL, ("judge", "JudgeScore", "LessThanThreshold", 3.5, False)],
}


class Serving(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        env_name: str,
        tenants: list[str],
        artifacts: s3.IBucket,
        key: kms.IKey,
        topic: sns.ITopic,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        api_key: secrets.ISecret,
        gateway_url: str,
        gateway_key: secrets.ISecret,
        knowledge_base_id: str,
        region: str,
        ops=None,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.prefix = prefix
        self.topic = topic

        # ----- one serving role per owner: reads only that owner's artifacts -----
        self.serving_roles: dict[str, iam.Role] = {}
        for owner in [*tenants, LIVE]:
            self.serving_roles[owner] = self._serving_role(owner, artifacts, key)
        self.serving_role = self.serving_roles[LIVE]
        self.preprocessor = s3deploy.BucketDeployment(
            self,
            "MonitorCode",
            sources=[
                s3deploy.Source.data(PREPROCESSOR_KEY, PREPROCESSOR.read_text(encoding="utf-8"))
            ],
            destination_bucket=artifacts,
            prune=False,
        )
        self.env_name = env_name

        # ----- live endpoint alarms: the pager and the canary's rollback -----
        self.endpoint_alarms: dict[str, list[cw.Alarm]] = {}
        self.rollback_alarms: dict[str, list[cw.Alarm]] = {}
        for project in PROJECTS:
            endpoint = f"{prefix}-{LIVE}-{project}"
            dims = {"EndpointName": endpoint, "VariantName": "AllTraffic"}
            errors = cw.Alarm(
                self,
                f"Alarm5xx{project.title()}",
                alarm_name=f"{endpoint}-5xx",
                metric=cw.Metric(
                    namespace="AWS/SageMaker",
                    metric_name="Invocation5XXErrors",
                    dimensions_map=dims,
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                threshold=5,
                evaluation_periods=2,
                alarm_description=f"{endpoint}: more than 5 server errors a minute, twice",
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            latency = cw.Alarm(
                self,
                f"AlarmLatency{project.title()}",
                alarm_name=f"{endpoint}-p95",
                metric=cw.Metric(
                    namespace="AWS/SageMaker",
                    metric_name="ModelLatency",
                    dimensions_map=dims,
                    statistic="p95",
                    period=Duration.minutes(1),
                ),
                threshold=2_000_000,  # microseconds
                evaluation_periods=3,
                alarm_description=f"{endpoint}: model latency p95 above 2 seconds for 3 minutes",
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            drift = cw.Alarm(
                self,
                f"AlarmDrift{project.title()}",
                alarm_name=f"{endpoint}-drift",
                metric=cw.Metric(
                    namespace="aws/sagemaker/Endpoints/data-metrics",
                    metric_name=f"feature_baseline_drift_{MONITOR_FEATURE}",
                    dimensions_map={
                        "Endpoint": endpoint,
                        "MonitoringSchedule": f"{endpoint}-data-quality",
                    },
                    statistic="Maximum",
                    period=Duration.hours(1),
                ),
                threshold=0.2,
                evaluation_periods=1,
                alarm_description=f"{endpoint}: Model Monitor baseline drift on {MONITOR_FEATURE}",
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            silent = cw.Alarm(
                self,
                f"AlarmMonitorSilent{project.title()}",
                alarm_name=f"{endpoint}-monitor-silent",
                metric=cw.Metric(
                    namespace="aws/sagemaker/Endpoints/data-metrics",
                    metric_name=f"feature_baseline_drift_{MONITOR_FEATURE}",
                    dimensions_map={
                        "Endpoint": endpoint,
                        "MonitoringSchedule": f"{endpoint}-data-quality",
                    },
                    statistic="SampleCount",
                    period=Duration.hours(1),
                ),
                threshold=1,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=3,
                alarm_description=(
                    f"{endpoint}: Model Monitor published nothing for 3 hours (no baseline, "
                    "no capture, a failed job, or the endpoint is stopped)"
                ),
                treat_missing_data=cw.TreatMissingData.BREACHING,
            )
            quality, quality_rollback = self.quality_alarms(project, env_name)
            for a in (errors, latency, drift, silent):
                a.add_alarm_action(cw_actions.SnsAction(topic))
            # Rollback on errors, latency and the quality level; not on the hourly monitor.
            self.endpoint_alarms[project] = [errors, latency, drift, silent, *quality]
            self.rollback_alarms[project] = [errors, latency, *quality_rollback]

        # ----- the deployer -----
        monitor_account = MONITOR_ACCOUNTS.get(region)
        monitor_image = (
            f"{monitor_account}.dkr.ecr.{region}.amazonaws.com/sagemaker-model-monitor-analyzer"
            if monitor_account
            else "unsupported-region"
        )
        self.deployer = lam.Function(
            self,
            "DeployOnApproval",
            function_name=f"{prefix}-deploy-on-approval",
            runtime=lam.Runtime.PYTHON_3_12,
            architecture=lam.Architecture.ARM_64,
            handler="handler.handler",
            code=lam.Code.from_asset(str(FUNCTIONS / "deploy_on_approval")),
            timeout=Duration.minutes(2),
            memory_size=256,
            description="Deploys every approved model package: serverless for a tenant, canary for live",
            environment={
                "NW_PREFIX": prefix,
                "NW_OWNERS": ",".join([*tenants, LIVE]),
                "NW_PROJECTS": ",".join(PROJECTS),
                "NW_SERVING_ROLE_ARNS": json.dumps(
                    {o: r.role_arn for o, r in self.serving_roles.items()}
                ),
                "NW_ARTIFACTS_BUCKET": artifacts.bucket_name,
                "NW_ALLOWED_IMAGES": json.dumps(allowed_image_prefixes(self, prefix)),
                "NW_CAPTURE_URI": f"s3://{artifacts.bucket_name}/capture",
                "NW_MONITOR_URI": f"s3://{artifacts.bucket_name}/monitoring",
                "NW_PREPROCESSOR_URI": f"s3://{artifacts.bucket_name}/{PREPROCESSOR_KEY}",
                "NW_MONITOR_IMAGE": monitor_image,
                "NW_KMS_KEY_ARN": key.key_arn,
                "NW_ROLLBACK_ALARMS": json.dumps(
                    {
                        p: [a.alarm_name for a in alarms]
                        for p, alarms in self.rollback_alarms.items()
                    }
                ),
            },
            log_group=logs.LogGroup(
                self,
                "DeployerLogs",
                log_group_name=f"/aws/lambda/{prefix}-deploy-on-approval",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self.deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="DeployPlatformEndpoints",
                actions=[
                    "sagemaker:CreateModel",
                    "sagemaker:CreateEndpointConfig",
                    "sagemaker:CreateEndpoint",
                    "sagemaker:UpdateEndpoint",
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DescribeEndpointConfig",
                    "sagemaker:DescribeModelPackage",
                    "sagemaker:CreateMonitoringSchedule",
                    "sagemaker:DescribeMonitoringSchedule",
                    "sagemaker:CreateDataQualityJobDefinition",
                    "sagemaker:DescribeDataQualityJobDefinition",
                    "sagemaker:AddTags",
                ],
                resources=[
                    sagemaker_arn(self, kind, f"{prefix}-*")
                    for kind in (
                        "model",
                        "endpoint-config",
                        "endpoint",
                        "model-package",
                        "monitoring-schedule",
                        "data-quality-job-definition",
                    )
                ],
            )
        )
        self.deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassOwnerServingRoles",
                actions=["iam:PassRole"],
                resources=[r.role_arn for r in self.serving_roles.values()],
                conditions={"StringEquals": {"iam:PassedToService": SAGEMAKER_SERVICE}},
            )
        )
        self.deployer.add_to_role_policy(instance_type_guard())
        self.deployer.add_to_role_policy(accelerator_guard())
        # A live promotion copies the tenant's artifact and baseline into platform prefixes.
        artifacts.grant_read(self.deployer, "tenants/*")
        artifacts.grant_put(self.deployer, "live/*")
        artifacts.grant_put(self.deployer, "monitoring/baselines/*")
        self.deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="Autoscaling",
                actions=[
                    "application-autoscaling:RegisterScalableTarget",
                    "application-autoscaling:PutScalingPolicy",
                    "application-autoscaling:DescribeScalableTargets",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": {"application-autoscaling:service-namespace": "sagemaker"}
                },
            )
        )
        self.deployer.add_to_role_policy(
            iam.PolicyStatement(
                sid="AutoscalingServiceLinkedRole",
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    f"arn:aws:iam::{stack.account}:role/aws-service-role/sagemaker.application-autoscaling.amazonaws.com/AWSServiceRoleForApplicationAutoScaling_SageMakerEndpoint"
                ],
                conditions={
                    "StringLike": {
                        "iam:AWSServiceName": "sagemaker.application-autoscaling.amazonaws.com"
                    }
                },
            )
        )
        key.grant_encrypt_decrypt(self.deployer)
        events.Rule(
            self,
            "OnApproval",
            rule_name=f"{prefix}-model-approved",
            description="Every approved model package of this platform reaches its endpoint",
            event_pattern=events.EventPattern(
                source=["aws.sagemaker"],
                detail_type=["SageMaker Model Package State Change"],
                detail={
                    "ModelApprovalStatus": ["Approved"],
                    # Exact names: `northwind-` as a prefix would also match every
                    # `northwind-staging-*` group of a second environment in the account.
                    "ModelPackageGroupName": [
                        f"{prefix}-{o}-{p}" for o in [*tenants, LIVE] for p in PROJECTS
                    ],
                },
            ),
            targets=[targets.LambdaFunction(self.deployer, retry_attempts=2)],
        )

        # ----- the policy service: inference Lambda behind the HTTP API -----
        name = "policy"
        app, artifacts_baked, hf, _cpu, _mem = SERVICES[name]
        fn_name = f"{prefix}-{name}"
        role = iam.Role(
            self,
            "PolicyRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"{fn_name} execution role",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSXrayWriteOnlyAccess"),
            ],
        )
        api_key.grant_read(role)
        gateway_key.grant_read(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RetrieveLive",
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/{knowledge_base_id}"
                ],
            )
        )
        if ops is not None:
            ops.grant_ops(role, LIVE)
        self.policy_fn = lam.DockerImageFunction(
            self,
            "FnPolicy",
            function_name=fn_name,
            code=lam.DockerImageCode.from_image_asset(
                directory=str(REPO_ROOT),
                file="Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
                build_args={
                    "APP": app,
                    "ARTIFACTS": artifacts_baked,
                    "HF_MODELS": hf,
                    "LAMBDA": "1",
                    "PORT": "8000",
                    "GIT_SHA": git_sha(),
                },
                exclude=IMAGE_EXCLUDE,
            ),
            architecture=lam.Architecture.ARM_64,
            memory_size=4096,
            timeout=Duration.seconds(120),
            ephemeral_storage_size=Size.mebibytes(1024),
            role=role,
            environment={
                "NW_TRACK": "aws",
                "NW_AWS_REGION": region,
                "NW_ENVIRONMENT": prefix,
                "NW_TENANT": LIVE,
                "NW_STAGE": env_name,
                "NW_LOG_FORMAT": "json",
                "NW_API_KEY_SECRET_ARN": api_key.secret_arn,
                "NW_GATEWAY_URL": gateway_url,
                "NW_GATEWAY_KEY_SECRET_ARN": gateway_key.secret_arn,
                "NW_KNOWLEDGE_BASE_ID": knowledge_base_id,
                "NW_TRACE_EXPORT": "xray",
                "NW_METRICS_FORMAT": "emf",
                "NW_SPEND_CAP_USD": "25",
                # One proxy in front: API Gateway puts the caller's address in X-Forwarded-For.
                "NW_TRUSTED_PROXY_HOPS": "1",
                "OTEL_SERVICE_NAME": fn_name,
                # Feedback verdicts go to the live owner's prefix of the ops bucket
                # (nw/agent/opstore.py), never to the sandbox's /tmp, which lasts one instance.
                **({"NW_OPS_STORE": ops.ops_uri} if ops is not None else {}),
                # Names in questions and notes are redacted before anything is kept.
                "NW_REDACT_DETECTOR": "heuristic",
                "HF_HOME": "/app/hf",
            },
            tracing=lam.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "PolicyLogs",
                log_group_name=f"/aws/lambda/{fn_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description=f"{fn_name}: the policy service as the RAG inference layer",
        )
        self.image_param = CfnParameter(
            self,
            "PolicyImage",
            type="AWS::SSM::Parameter::Value<String>",
            default=image_parameter(prefix, "policy"),
            description="The policy image the delivery pipeline promoted, or `asset` for the image this stack builds",
        )
        self.image_param.override_logical_id("PolicyImage")
        use_asset = CfnCondition(
            self,
            "PolicyUsesAsset",
            expression=Fn.condition_equals(self.image_param.value_as_string, "asset"),
        )
        use_asset.override_logical_id("PolicyUsesAsset")
        cfn_fn = self.policy_fn.node.default_child
        assert isinstance(cfn_fn, lam.CfnFunction)
        asset_uri = stack.resolve(cfn_fn.code)["imageUri"]
        cfn_fn.add_property_override(
            "Code.ImageUri",
            Fn.condition_if(use_asset.logical_id, asset_uri, self.image_param.value_as_string),
        )
        self.policy_alias = lam.Alias(
            self, "AliasPolicy", alias_name="live", version=self.policy_fn.current_version
        )
        errors = cw.Alarm(
            self,
            "AlarmPolicyErrors",
            alarm_name=f"{fn_name}-errors",
            metric=self.policy_fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            threshold=5,
            evaluation_periods=2,
            alarm_description=f"{fn_name}: more than 5 errors in 5 minutes, twice",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        latency = cw.Alarm(
            self,
            "AlarmPolicyLatency",
            alarm_name=f"{fn_name}-p95",
            metric=self.policy_fn.metric_duration(period=Duration.minutes(5), statistic="p95"),
            threshold=8000,
            evaluation_periods=3,
            alarm_description=f"{fn_name}: p95 duration above 8 seconds for 15 minutes",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        drift_metric = logs.MetricFilter(
            self,
            "PolicyDriftFilter",
            log_group=self.policy_fn.log_group,
            metric_namespace="Northwind",
            metric_name=f"DriftAlerts-{env_name}-policy" if env_name else "DriftAlerts-policy",
            filter_pattern=logs.FilterPattern.string_value("$.msg", "=", "drift_alert"),
            metric_value="1",
        ).metric(period=Duration.minutes(5), statistic="Sum")
        drift = cw.Alarm(
            self,
            "AlarmPolicyDrift",
            alarm_name=f"{fn_name}-drift",
            metric=drift_metric,
            threshold=1,
            evaluation_periods=1,
            alarm_description=f"{fn_name}: retrieval drift signal past its bar",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        for a in (errors, latency, drift):
            a.add_alarm_action(cw_actions.SnsAction(topic))
        policy_quality, policy_quality_rollback = self.quality_alarms("policy", env_name)
        self.policy_alarms = [errors, latency, drift, *policy_quality]
        self.quality_alert_filter(self.policy_fn.log_group, "policy", env_name)
        self.deploy_app = cd.LambdaApplication(self, "Deploy", application_name=f"{prefix}-lambda")
        self.policy_group = cd.LambdaDeploymentGroup(
            self,
            "DeployPolicy",
            application=self.deploy_app,
            deployment_group_name=fn_name,
            alias=self.policy_alias,
            deployment_config=DEPLOYMENT_CONFIG,
            alarms=[errors, latency, *policy_quality_rollback],
            auto_rollback=cd.AutoRollbackConfig(
                failed_deployment=True, stopped_deployment=True, deployment_in_alarm=True
            ),
        )

        authorizer = authorizers.HttpUserPoolAuthorizer(
            "Cognito", user_pool, user_pool_clients=[user_pool_client]
        )
        integration = integrations.HttpLambdaIntegration("Policy", self.policy_alias)
        self.api = apigw.HttpApi(
            self,
            "PolicyApi",
            api_name=f"{prefix}-policy",
            description="The policy service behind Cognito: the web entry of the LLMOps reference",
            create_default_stage=False,
            default_authorizer=authorizer,
        )
        self.api.add_routes(
            path="/{proxy+}", methods=[apigw.HttpMethod.ANY], integration=integration
        )
        for probe in ("/healthz", "/readyz"):
            self.api.add_routes(
                path=probe,
                methods=[apigw.HttpMethod.GET],
                integration=integration,
                authorizer=apigw.HttpNoneAuthorizer(),
            )
        api_logs = logs.LogGroup(
            self,
            "ApiLogs",
            log_group_name=f"/{prefix}/policy-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.stage = apigw.HttpStage(
            self,
            "PolicyStage",
            http_api=self.api,
            stage_name="v1",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=50, burst_limit=100),
        )
        cfn_stage = self.stage.node.default_child
        assert isinstance(cfn_stage, apigw.CfnStage)
        cfn_stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=api_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "ip": "$context.identity.sourceIp",
                    "requestTime": "$context.requestTime",
                    "httpMethod": "$context.httpMethod",
                    "routeKey": "$context.routeKey",
                    "status": "$context.status",
                    "responseLength": "$context.responseLength",
                    "integrationError": "$context.integrationErrorMessage",
                }
            ),
        )
        CfnOutput(self, "OutUrlPolicy", value=self.stage.url).override_logical_id("UrlPolicy")
        CfnOutput(
            self, "OutDeployApplication", value=self.deploy_app.application_name
        ).override_logical_id("DeployApplication")
        CfnOutput(self, "OutServingRoleArn", value=self.serving_role.role_arn).override_logical_id(
            "ServingRoleArn"
        )
        CfnOutput(
            self,
            "OutServingRoleArns",
            value=",".join(f"{o}={r.role_arn}" for o, r in self.serving_roles.items()),
        ).override_logical_id("ServingRoleArns")

    def quality_alarms(self, service: str, env_name: str) -> tuple[list[cw.Alarm], list[cw.Alarm]]:
        """Every quality alarm of a service (each pages the topic) and the ones a canary rolls
        back on."""
        out, rollback = [], []
        stage = env_name or "default"
        for name, metric, comparison, threshold, rolls_back in QUALITY_ALARMS.get(service, []):
            alarm = cw.Alarm(
                self,
                f"AlarmQuality{service.title()}{name.title().replace('-', '')}",
                alarm_name=f"{self.prefix}-{service}-quality-{name}",
                metric=cw.Metric(
                    namespace="Northwind",
                    metric_name=metric,
                    dimensions_map={"Service": service, "Stage": stage},
                    # A gauge: the worst value any instance reported in the period.
                    statistic="Minimum" if comparison.startswith("Less") else "Maximum",
                    period=Duration.minutes(5),
                ),
                threshold=threshold,
                comparison_operator=COMPARISONS[comparison],
                evaluation_periods=2,
                alarm_description=f"{service}: {metric} {comparison} {threshold} (deploy/SLO.md)"
                + (", a canary rolls back" if rolls_back else ", pages only"),
                treat_missing_data=cw.TreatMissingData.MISSING,
            )
            alarm.add_alarm_action(cw_actions.SnsAction(self.topic))
            out.append(alarm)
            if rolls_back:
                rollback.append(alarm)
        return out, rollback

    def quality_alert_filter(self, log_group: logs.ILogGroup, service: str, env_name: str) -> None:
        """`quality_alert` lines (nw/quality.py) counted, and an alarm on any. The line carries
        the signal, value and bar; Logs Insights on the group shows which one moved."""
        name = f"QualityAlerts-{env_name}-{service}" if env_name else f"QualityAlerts-{service}"
        metric = logs.MetricFilter(
            self,
            f"QualityFilter{service.title()}",
            log_group=log_group,
            metric_namespace="Northwind",
            metric_name=name,
            filter_pattern=logs.FilterPattern.string_value("$.msg", "=", "quality_alert"),
            metric_value="1",
        ).metric(period=Duration.minutes(5), statistic="Sum")
        alarm = cw.Alarm(
            self,
            f"AlarmQualityAlerts{service.title()}",
            alarm_name=f"{self.prefix}-{service}-quality-alert",
            metric=metric,
            threshold=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            alarm_description=f"{service}: a monitor logged quality_alert (the line names the signal)",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.topic))

    def _serving_role(self, owner: str, artifacts: s3.IBucket, key: kms.IKey) -> iam.Role:
        """What an endpoint (and, for live, its monitor) needs, for one owner."""
        stack = Stack.of(self)
        role = iam.Role(
            self,
            f"ServingRole{owner.title()}",
            role_name=f"{self.prefix}-{owner}-serving",
            assumed_by=iam.ServicePrincipal(SAGEMAKER_SERVICE),
            description=f"Endpoints for {owner}: pull the image, read {owner}'s artifacts"
            + (", write capture and monitoring" if owner == LIVE else ""),
        )
        if owner == LIVE:
            artifacts.grant_read(role, "live/*")
            artifacts.grant_read_write(role, "capture/*")
            artifacts.grant_read_write(role, "monitoring/*")
        else:
            artifacts.grant_read(role, f"tenants/{owner}/*")
        key.grant_encrypt_decrypt(role)
        role.add_to_policy(ecr_pull_statement(self, self.prefix))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrToken", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Logs",
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
                sid="MonitorMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringLike": {
                        "cloudwatch:namespace": [
                            "aws/sagemaker/Endpoints/data-metrics",
                            "/aws/sagemaker/*",
                        ]
                    }
                },
            )
        )
        return role
