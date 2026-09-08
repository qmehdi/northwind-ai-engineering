"""The Session path on AWS: four App Runner services from the course images.

Each participant deploys this in Session 6. Least-privilege instance roles: only
the policy and agent services may invoke models, and only the three course
models. Health checks hit /readyz so a service with no model never receives
traffic. X-Ray tracing on, one dashboard, alarms to SNS, a monthly budget.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apprunner_alpha as apprunner
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from constructs import Construct

from stacks.common import (
    SERVICES,
    alerts_topic,
    bedrock_invoke_policy,
    dashboard,
    image,
    monthly_budget,
    service_alarms,
)

CPU = {1: apprunner.Cpu.ONE_VCPU, 2: apprunner.Cpu.TWO_VCPU}
MEMORY = {2: apprunner.Memory.TWO_GB, 3: apprunner.Memory.THREE_GB, 4: apprunner.Memory.FOUR_GB}


class SessionPath(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        region: str,
        budget_usd: float,
        alert_email: str | None,
        paused: bool = False,
    ) -> None:
        super().__init__(scope, id)
        topic = alerts_topic(self, alert_email)
        self.services: dict[str, apprunner.Service] = {}
        self.urls: dict[str, str] = {}
        observability = apprunner.ObservabilityConfiguration(
            self, "Tracing", trace_configuration_vendor=apprunner.TraceConfigurationVendor.AWSXRAY
        )
        # Autoscaling: one instance minimum keeps the models warm; paused stacks drop to zero
        # by deleting the service, which the Makefile does through `stop`.
        scaling = apprunner.AutoScalingConfiguration(
            self, "Scaling", min_size=1, max_size=3, max_concurrency=20
        )

        for name, (_app, _artifacts, _hf, cpu, mem) in SERVICES.items():
            role = iam.Role(
                self,
                f"InstanceRole{name.title()}",
                assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
                description=f"northwind {name} instance role",
            )
            if name in ("policy", "agent"):
                role.add_to_policy(bedrock_invoke_policy(self))
            env = {
                "NW_TRACK": "aws",
                "NW_AWS_REGION": region,
                "NW_LOG_FORMAT": "json",
                "PORT": "8000",
            }
            if name == "agent":
                env["NW_AGENT_ROLE"] = "resolver"
            asset = image(self, name, platform=ecr_assets.Platform.LINUX_AMD64)
            service = apprunner.Service(
                self,
                f"Service{name.title()}",
                service_name=f"northwind-{name}",
                source=apprunner.Source.from_asset(
                    asset=asset,
                    image_configuration=apprunner.ImageConfiguration(
                        port=8000, environment_variables=env
                    ),
                ),
                cpu=CPU[cpu],
                memory=MEMORY[mem],
                instance_role=role,
                health_check=apprunner.HealthCheck.http(
                    path="/readyz",
                    interval=Duration.seconds(10),
                    timeout=Duration.seconds(5),
                    healthy_threshold=1,
                    unhealthy_threshold=5,
                ),
                observability_configuration=observability,
                auto_scaling_configuration=scaling,
                auto_deployments_enabled=False,
            )
            self.services[name] = service
            self.urls[name] = f"https://{service.service_url}"
            service_alarms(self, f"northwind-{name}", topic)
            CfnOutput(self, f"Url{name.title()}", value=f"https://{service.service_url}")
        dashboard(self, [f"northwind-{n}" for n in SERVICES])
        monthly_budget(self, limit_usd=budget_usd, email=alert_email)


class SessionStack(Stack):
    def __init__(
        self, scope: Construct, id: str, *, budget_usd: float, alert_email: str | None, **kw
    ) -> None:
        super().__init__(scope, id, **kw)
        self.path = SessionPath(
            self, "Session", region=self.region, budget_usd=budget_usd, alert_email=alert_email
        )
