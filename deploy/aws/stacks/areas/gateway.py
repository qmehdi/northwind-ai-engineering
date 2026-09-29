"""The model gateway: LiteLLM on ECS Fargate behind an ALB, after the AWS multi-provider
generative AI gateway guidance (LiteLLM, RDS for virtual keys, ALB, Secrets Manager for the
master key; the guidance's Redis cache and CloudFront are left out, they add nothing the course
measures).

- One application inference profile per tenant and role, copied from the course model, tagged
  `nw:tenant`: Bedrock reports cost and tokens per profile, so the cost line per learner comes
  from Cost Explorer with no code.
- LiteLLM's config is generated here (model names `<tenant>/workhorse|judge|economy` and
  `workhorse|judge|economy` for the live services, each routed to the right profile ARN) and
  written to the artifacts bucket; the proxy loads it at start through
  `LITELLM_CONFIG_BUCKET_NAME` and `LITELLM_CONFIG_BUCKET_OBJECT_KEY` (litellm
  `proxy_server.py`, read 2026-09-29).
- Virtual keys need Postgres: Aurora Serverless v2 with a 0 ACU floor pauses when idle. LiteLLM
  assembles `DATABASE_URL` from `DATABASE_HOST` (host:port), `DATABASE_USERNAME`,
  `DATABASE_PASSWORD` and `DATABASE_NAME` (litellm `proxy/utils.py`, read 2026-09-29).
- Per-tenant virtual keys with a budget are created by `deploy/aws/scripts/gateway_keys.sh`
  after the deploy (`POST /key/generate` with the master key), and stored in Secrets Manager as
  `northwind-<tenant>-gateway-key`.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

from stacks.common import LIVE, MODEL_IDS, bedrock_invoke_policy

LITELLM_IMAGE = "ghcr.io/berriai/litellm:main-stable"
CONFIG_KEY = "gateway/litellm.yaml"


class ModelGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tenants: list[str],
        vpc: ec2.IVpc,
        artifacts: s3.IBucket,
        logs_bucket: s3.IBucket,
        key: kms.IKey,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)

        # ----- inference profiles: the cost line per tenant -----
        self.profiles: dict[tuple[str, str], bedrock.CfnApplicationInferenceProfile] = {}
        for owner in [*tenants, LIVE]:
            for role, model_id in MODEL_IDS.items():
                self.profiles[(owner, role)] = bedrock.CfnApplicationInferenceProfile(
                    self,
                    f"Profile{owner.title()}{role.title()}",
                    inference_profile_name=f"{prefix}-{owner}-{role}",
                    description=f"{role} model for {owner}",
                    model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                        copy_from=f"arn:aws:bedrock:{stack.region}::foundation-model/{model_id}"
                    ),
                    tags=[
                        {"key": "nw:tenant", "value": owner},
                        {"key": "nw:role", "value": role},
                    ],
                )

        # ----- the master key and the database -----
        self.master_key = sm.Secret(
            self,
            "MasterKey",
            description=f"{prefix} model gateway master key (LITELLM_MASTER_KEY)",
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=48
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.db = rds.DatabaseCluster(
            self,
            "KeysDb",
            cluster_identifier=f"{prefix}-gateway",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_6
            ),
            writer=rds.ClusterInstance.serverless_v2("writer", publicly_accessible=False),
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=1,
            serverless_v2_auto_pause_duration=Duration.minutes(15),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            default_database_name="litellm",
            credentials=rds.Credentials.from_generated_secret("litellm"),
            storage_encrypted=True,
            storage_encryption_key=key,
            iam_authentication=True,
            port=5433,
            backup=rds.BackupProps(retention=Duration.days(7)),
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ----- the proxy -----
        cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=f"{prefix}-gateway",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/{prefix}/gateway",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        db_secret = self.db.secret
        assert db_secret is not None
        self.service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            service_name=f"{prefix}-gateway",
            load_balancer_name=f"{prefix}-gateway",
            cpu=512,
            memory_limit_mib=1024,
            desired_count=1,
            public_load_balancer=True,
            assign_public_ip=True,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry(LITELLM_IMAGE),
                container_port=4000,
                container_name="litellm",
                environment={
                    "LITELLM_CONFIG_BUCKET_NAME": artifacts.bucket_name,
                    "LITELLM_CONFIG_BUCKET_OBJECT_KEY": CONFIG_KEY,
                    "DATABASE_HOST": f"{self.db.cluster_endpoint.hostname}:{self.db.cluster_endpoint.port}",
                    "DATABASE_NAME": "litellm",
                    "AWS_REGION_NAME": stack.region,
                    "LITELLM_LOG": "INFO",
                    "STORE_MODEL_IN_DB": "False",
                },
                secrets={
                    "LITELLM_MASTER_KEY": ecs.Secret.from_secrets_manager(self.master_key),
                    "DATABASE_USERNAME": ecs.Secret.from_secrets_manager(db_secret, "username"),
                    "DATABASE_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
                },
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="litellm", log_group=log_group),
            ),
        )
        # The pattern's own outputs duplicate GatewayUrl under generated names.
        for child in ("LoadBalancerDNS", "ServiceURL"):
            self.service.node.try_remove_child(child)
        self.service.target_group.configure_health_check(
            path="/health/liveliness", interval=Duration.seconds(30), healthy_threshold_count=2
        )
        self.service.load_balancer.log_access_logs(logs_bucket, "gateway-alb")
        self.db.connections.allow_default_port_from(self.service.service, "LiteLLM virtual keys")
        task_role = self.service.task_definition.task_role
        task_role.add_to_principal_policy(bedrock_invoke_policy(self))
        artifacts.grant_read(task_role, CONFIG_KEY)
        key.grant_decrypt(task_role)

        # ----- the config, generated with the profile ARNs -----
        lines = ["model_list:"]
        for (owner, role), profile in self.profiles.items():
            name = role if owner == LIVE else f"{owner}/{role}"
            lines += [
                f"  - model_name: {name}",
                "    litellm_params:",
                f"      model: bedrock/{profile.attr_inference_profile_arn}",
                f"      aws_region_name: {stack.region}",
                "    model_info:",
                f"      nw_tenant: {owner}",
                f"      nw_role: {role}",
            ]
        lines += [
            "litellm_settings:",
            "  drop_params: true",
            "  turn_off_message_logging: true",
            "general_settings:",
            "  master_key: os.environ/LITELLM_MASTER_KEY",
            "  disable_spend_logs: false",
        ]
        self.config = s3deploy.BucketDeployment(
            self,
            "Config",
            sources=[s3deploy.Source.data(CONFIG_KEY, "\n".join(lines) + "\n")],
            destination_bucket=artifacts,
            prune=False,
        )
        self.service.service.node.add_dependency(self.config)

        self.url = f"http://{self.service.load_balancer.load_balancer_dns_name}"
        CfnOutput(self, "OutGatewayUrl", value=self.url).override_logical_id("GatewayUrl")
        CfnOutput(
            self, "OutGatewayMasterKeyArn", value=self.master_key.secret_arn
        ).override_logical_id("GatewayMasterKeyArn")
        CfnOutput(
            self,
            "OutInferenceProfiles",
            value=",".join(
                f"{o}/{r}={p.attr_inference_profile_arn}" for (o, r), p in self.profiles.items()
            ),
        ).override_logical_id("InferenceProfiles")

    @property
    def alb(self) -> elbv2.IApplicationLoadBalancer:
        return self.service.load_balancer

    def grant_use(self, grantee: iam.IGrantable) -> None:
        """Read the master key: the key bootstrap script and the delivery deployer."""
        self.master_key.grant_read(grantee)
