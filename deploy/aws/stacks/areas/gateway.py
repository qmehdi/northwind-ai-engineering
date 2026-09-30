"""The model gateway: LiteLLM on ECS Fargate behind an ALB and CloudFront, after the AWS
multi-provider generative AI gateway guidance (LiteLLM, RDS for virtual keys, ALB, CloudFront,
Secrets Manager for the master key; the guidance's Redis cache is left out).

- HTTPS: learners call `https://<distribution>.cloudfront.net` (CloudFront's default
  certificate, no domain needed). The ALB accepts traffic only from CloudFront's origin-facing
  managed prefix list (`com.amazonaws.global.cloudfront.origin-facing`, looked up at deploy) and
  forwards only requests carrying the secret `X-Origin-Verify` header CloudFront adds; every
  other request gets 403. The CloudFront to ALB leg is HTTP inside AWS's network; an
  organisation with a domain adds an ACM certificate to the ALB and sets the origin to HTTPS
  only (README, "Networking"). Streaming works; the origin read timeout is 60 seconds, the
  default quota's maximum.
- The LiteLLM image is pinned by tag and digest, the same pin as the Local compose file.
- `LITELLM_SALT_KEY` (encrypts the model credentials LiteLLM stores in its database) is a
  generated secret, as the LiteLLM production checklist asks.

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
- Per-tenant virtual keys with a budget are minted by `deploy/aws/scripts/gateway_keys.sh`
  after the deploy (`POST /key/generate` with the master key) and written into the secrets
  `northwind-<owner>-gateway-key`, which this stack owns, so `cdk destroy` and removing a tenant
  delete them.
- The application inference profiles copy from the system cross-region profile where the model
  card requires one (`stacks/common.py`, `copy_from_arn`), and LiteLLM calls them through the
  Converse route (`bedrock/converse/<profile arn>`), which is model-agnostic.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
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
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks.common import (
    EU_MODEL_IDS,
    EU_REGION,
    GATEWAY_EU_PREFIX,
    LIVE,
    MODEL_IDS,
    bedrock_invoke_policy,
    copy_from_arn,
)

# Tag and digest, the same pin as docker-compose.yml's `litellm` service (the Local track).
LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm:v1.103.0"
    "@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7"
)
CONFIG_KEY = "gateway/litellm.yaml"
ORIGIN_HEADER = "X-Origin-Verify"
CLOUDFRONT_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"


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
            for role in MODEL_IDS:
                self.profiles[(owner, role)] = bedrock.CfnApplicationInferenceProfile(
                    self,
                    f"Profile{owner.title()}{role.title()}",
                    inference_profile_name=f"{prefix}-{owner}-{role}",
                    description=f"{role} model for {owner}",
                    model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                        copy_from=copy_from_arn(role, stack.region, stack.account)
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
        self.salt_key = sm.Secret(
            self,
            "SaltKey",
            description=f"{prefix} model gateway salt key (LITELLM_SALT_KEY); never rotate once keys exist",
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=48
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.origin_secret = sm.Secret(
            self,
            "OriginSecret",
            description=f"{prefix} header value CloudFront adds and the gateway ALB requires",
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=40
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The virtual key secrets, one per owner, filled by gateway_keys.sh.
        self.key_secrets: dict[str, sm.Secret] = {}
        for owner in tenants:
            self.key_secrets[owner] = sm.Secret(
                self,
                f"GatewayKey{owner.title()}",
                secret_name=f"{prefix}-{owner}-gateway-key",
                description=f"{prefix} model gateway virtual key for {owner} (filled by gateway_keys.sh)",
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
            open_listener=False,
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
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                },
                secrets={
                    "LITELLM_MASTER_KEY": ecs.Secret.from_secrets_manager(self.master_key),
                    "LITELLM_SALT_KEY": ecs.Secret.from_secrets_manager(self.salt_key),
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
                f"      model: bedrock/converse/{profile.attr_inference_profile_arn}",
                f"      aws_region_name: {stack.region}",
                "    model_info:",
                f"      nw_tenant: {owner}",
                f"      nw_role: {role}",
            ]
        # The EU residency route (`nw/config.py` EU_MODELS): the same roles, invoked in the EU
        # region by model or EU geo profile id. Cost per tenant still shows in the gateway's
        # spend logs; Bedrock's per-profile metrics cover the default route only.
        for owner in [*tenants, LIVE]:
            for role, model_id in EU_MODEL_IDS.items():
                name = (
                    f"{GATEWAY_EU_PREFIX}{role}"
                    if owner == LIVE
                    else f"{GATEWAY_EU_PREFIX}{owner}/{role}"
                )
                lines += [
                    f"  - model_name: {name}",
                    "    litellm_params:",
                    f"      model: bedrock/converse/{model_id}",
                    f"      aws_region_name: {EU_REGION}",
                    "    model_info:",
                    f"      nw_tenant: {owner}",
                    f"      nw_role: {role}",
                    "      nw_residency: eu",
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

        self._front(prefix, logs_bucket)
        self.url = f"https://{self.distribution.distribution_domain_name}"
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

    def _front(self, prefix: str, logs_bucket: s3.IBucket) -> None:
        """CloudFront in front, the ALB open only to CloudFront and only with the header."""
        lookup = cr.AwsCustomResource(
            self,
            "CloudFrontPrefixList",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="describeManagedPrefixLists",
                parameters={
                    "Filters": [{"Name": "prefix-list-name", "Values": [CLOUDFRONT_PREFIX_LIST]}]
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"{prefix}-{CLOUDFRONT_PREFIX_LIST}"),
                output_paths=["PrefixLists.0.PrefixListId"],
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [iam.PolicyStatement(actions=["ec2:DescribeManagedPrefixLists"], resources=["*"])]
            ),
            install_latest_aws_sdk=False,
        )
        prefix_list = lookup.get_response_field("PrefixLists.0.PrefixListId")
        alb_sg = self.service.load_balancer.connections.security_groups[0]
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(prefix_list), ec2.Port.tcp(80), "CloudFront origin-facing only"
        )
        header_value = self.origin_secret.secret_value.unsafe_unwrap()  # a dynamic reference
        listener = self.service.listener
        listener.add_action(
            "FromCloudFront",
            priority=1,
            conditions=[elbv2.ListenerCondition.http_header(ORIGIN_HEADER, [header_value])],
            action=elbv2.ListenerAction.forward([self.service.target_group]),
        )
        cfn_listener = listener.node.default_child
        assert isinstance(cfn_listener, elbv2.CfnListener)
        cfn_listener.add_property_override(
            "DefaultActions",
            [
                {
                    "Type": "fixed-response",
                    "FixedResponseConfig": {
                        "StatusCode": "403",
                        "ContentType": "text/plain",
                        "MessageBody": "forbidden",
                    },
                }
            ],
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{prefix} model gateway (HTTPS)",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.LoadBalancerV2Origin(
                    self.service.load_balancer,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    origin_ssl_protocols=[cloudfront.OriginSslPolicy.TLS_V1_2],
                    http_port=80,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(60),
                    custom_headers={ORIGIN_HEADER: header_value},
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            enable_logging=True,
            log_bucket=logs_bucket,
            log_file_prefix="gateway-cloudfront/",
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )
        CfnOutput(
            self, "OutGatewayOrigin", value=self.service.load_balancer.load_balancer_dns_name
        ).override_logical_id("GatewayOrigin")

    @property
    def domain(self) -> str:
        return self.distribution.distribution_domain_name

    @property
    def alb(self) -> elbv2.IApplicationLoadBalancer:
        return self.service.load_balancer

    def grant_use(self, grantee: iam.IGrantable) -> None:
        """Read the master key: the key bootstrap script and the delivery deployer."""
        self.master_key.grant_read(grantee)

    def key_secret(self, owner: str) -> sm.ISecret:
        return self.key_secrets[owner]
