"""The platform stack: one environment in one account (ADR 0008), every area a construct.

    northwind-platform            the default environment
    northwind-platform-<env>      a second environment from the same code (-c env=<word>)

Order matters only where an area reads another: network and data first, identity (Cognito and
the alerts topic) before serving and agents, the model gateway before the services that call
it, delivery after the things it promotes, observability last.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

from stacks.areas.agents import Agents
from stacks.areas.data import DataGovernance
from stacks.areas.delivery import Delivery
from stacks.areas.gateway import ModelGateway
from stacks.areas.identity import Identity, Observability
from stacks.areas.network import Network
from stacks.areas.pipelines import Pipelines
from stacks.areas.prompts import PromptsRetrieval
from stacks.areas.serving import Serving
from stacks.areas.tracking import TrackingRegistry
from stacks.common import LIVE, MODEL_IDS, check_stage, prefix


class PlatformStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        env_name: str,
        tenants: list[str],
        mode: str,
        budget_usd: float,
        alert_email: str | None,
        connection_arn: str | None,
        github_owner: str,
        github_repo: str,
        github_branch: str,
        lake_formation: bool,
        **kw,
    ) -> None:
        super().__init__(scope, id, **kw)
        env_name = check_stage(env_name)
        pre = prefix(env_name)
        self.tenants = tenants

        network = Network(self, "Network", prefix=pre)
        data = DataGovernance(self, "Data", prefix=pre, lake_formation=lake_formation)
        identity = Identity(
            self, "Identity", prefix=pre, env_name=env_name, alert_email=alert_email
        )

        # One API key for the services (`x-api-key`), read by ARN, never in an image or a template.
        api_key = sm.Secret(
            self,
            "ApiKey",
            description=f"{pre} service API key",
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=40
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The live services' virtual key on the model gateway: written by gateway_keys.sh.
        gateway_key = sm.Secret(
            self,
            "LiveGatewayKey",
            secret_name=f"{pre}-{LIVE}-gateway-key",
            description=f"{pre} model gateway virtual key for the live services (filled by gateway_keys.sh)",
            removal_policy=RemovalPolicy.DESTROY,
        )

        tracking = TrackingRegistry(
            self,
            "Tracking",
            prefix=pre,
            tenants=tenants,
            vpc=network.vpc,
            data=data.data,
            artifacts=data.artifacts,
            key=data.key,
        )
        pipelines = Pipelines(
            self,
            "Pipelines",
            prefix=pre,
            tenants=tenants,
            data=data.data,
            artifacts=data.artifacts,
        )
        prompts = PromptsRetrieval(
            self, "Prompts", prefix=pre, tenants=tenants, data=data.data, key=data.key
        )
        gateway = ModelGateway(
            self,
            "Gateway",
            prefix=pre,
            tenants=tenants,
            vpc=network.vpc,
            artifacts=data.artifacts,
            logs_bucket=data.logs,
            key=data.key,
        )
        live_kb = prompts.knowledge_bases[LIVE].attr_knowledge_base_id
        serving = Serving(
            self,
            "Serving",
            prefix=pre,
            env_name=env_name,
            artifacts=data.artifacts,
            key=data.key,
            topic=identity.topic,
            user_pool=identity.user_pool,
            user_pool_client=identity.client,
            api_key=api_key,
            gateway_url=gateway.url,
            gateway_key=gateway_key,
            knowledge_base_id=live_kb,
            region=self.region,
        )
        agents = Agents(
            self,
            "Agents",
            prefix=pre,
            env_name=env_name,
            tenants=tenants,
            api_key=api_key,
            gateway_url=gateway.url,
            gateway_key=gateway_key,
            knowledge_base_id=live_kb,
            topic=identity.topic,
            cognito_domain_url=identity.domain_url,
        )
        delivery = Delivery(
            self,
            "Delivery",
            prefix=pre,
            key=data.key,
            logs_bucket=data.logs,
            topic=identity.topic,
            policy_fn=serving.policy_fn,
            deploy_app=serving.deploy_app,
            deploy_group=serving.policy_group,
            agent_runtime_arn=agents.agent_runtime.attr_agent_runtime_arn,
            agent_runtime_id=agents.agent_runtime.attr_agent_runtime_id,
            runtime_role=agents.runtime_role,
            connection_arn=connection_arn,
            github_owner=github_owner,
            github_repo=github_repo,
            github_branch=github_branch,
        )
        # The deployer and the tenants' roles may read the gateway master key to mint virtual keys.
        gateway.grant_use(delivery.deployer)
        Observability(
            self,
            "Observability",
            prefix=pre,
            env_name=env_name,
            key=data.key,
            logs_bucket=data.logs,
            topic=identity.topic,
            budget_usd=budget_usd,
            alert_email=alert_email,
            tenants=tenants,
            policy_fn=serving.policy_fn,
            endpoint_alarms=serving.endpoint_alarms,
            gateway=gateway,
            agents=agents,
            profiles=gateway.profiles,
        )
        self.areas = {
            "network": network,
            "data": data,
            "identity": identity,
            "tracking": tracking,
            "pipelines": pipelines,
            "prompts": prompts,
            "gateway": gateway,
            "serving": serving,
            "agents": agents,
            "delivery": delivery,
        }
        CfnOutput(self, "Environment", value=pre).override_logical_id("Environment")
        CfnOutput(self, "Mode", value=mode).override_logical_id("Mode")
        CfnOutput(self, "ApiKeySecretArn", value=api_key.secret_arn).override_logical_id(
            "ApiKeySecretArn"
        )
        CfnOutput(self, "LiveGatewayKeyArn", value=gateway_key.secret_arn).override_logical_id(
            "LiveGatewayKeyArn"
        )
        CfnOutput(self, "DataBucket", value=data.data.bucket_name).override_logical_id("DataBucket")
        CfnOutput(self, "ArtifactsBucket", value=data.artifacts.bucket_name).override_logical_id(
            "ArtifactsBucket"
        )
        CfnOutput(
            self, "Models", value=",".join(f"{r}={m}" for r, m in MODEL_IDS.items())
        ).override_logical_id("Models")
