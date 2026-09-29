"""The one VPC the platform needs: SageMaker Studio's EFS traffic, the model gateway's Fargate
tasks and its database. Public subnets only and no NAT gateway: the tasks get a public IP and
talk to Bedrock over the internet, which costs nothing while idle. Flow logs go to CloudWatch."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct


class Network(Construct):
    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        flow_logs = logs.LogGroup(
            self,
            "FlowLogs",
            log_group_name=f"/{prefix}/vpc-flow-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{prefix}-platform",
            max_azs=2,
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
            flow_logs={
                "all": ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs),
                    traffic_type=ec2.FlowLogTrafficType.REJECT,
                )
            },
        )
