"""Networks.

- `Network`: the platform VPC for SageMaker Studio's EFS traffic, the model gateway's Fargate
  tasks and its database. Public subnets only and no NAT gateway: the tasks get a public IP and
  talk to Bedrock over the internet, which costs nothing while idle. Flow logs go to CloudWatch.
  Public endpoints behind keys are a documented course simplification (README, "Networking").
- `AgentNetwork`: the egress control for the agents (the Agentic AI Lens and the AgentCore VPC
  guide). The runtimes run in VPC mode in two private subnets whose only way out is one NAT
  gateway, behind a security group that allows HTTPS out and nothing else, and a Route 53
  Resolver DNS Firewall that answers NXDOMAIN for every name outside an allow-list (AWS service
  endpoints under `amazonaws.com` and the model gateway's CloudFront name). AgentCore VPC mode
  only accepts subnets in listed Availability Zone IDs (devguide agentcore-vpc, fetched
  2026-09-30), so the private subnets are placed by AZ ID, not by AZ name. An S3 gateway
  endpoint (free) keeps the ECR layer pulls off the NAT gateway, as the guide recommends.
  What the DNS allow-list does not stop is a connection to a literal IP address on 443; a
  domain-aware firewall (AWS Network Firewall with SNI rules) closes that and is priced in
  `deploy/COSTS-platform.md`, not deployed.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53resolver as resolver
from constructs import Construct

# AgentCore Runtime VPC connectivity: supported AZ IDs per region (devguide agentcore-vpc,
# "Supported Availability Zones", fetched 2026-09-30). The first two are used.
AGENTCORE_AZ_IDS = {
    "us-east-1": ("use1-az1", "use1-az2", "use1-az4"),
    "us-east-2": ("use2-az1", "use2-az2", "use2-az3"),
    "us-west-1": ("usw1-az1", "usw1-az3"),
    "us-west-2": ("usw2-az1", "usw2-az2", "usw2-az3"),
    "ca-central-1": ("cac1-az1", "cac1-az2", "cac1-az4"),
    "eu-central-1": ("euc1-az1", "euc1-az2", "euc1-az3"),
    "eu-west-1": ("euw1-az1", "euw1-az2", "euw1-az3"),
    "eu-west-2": ("euw2-az1", "euw2-az2", "euw2-az3"),
    "eu-west-3": ("euw3-az1", "euw3-az2", "euw3-az3"),
    "eu-north-1": ("eun1-az1", "eun1-az2", "eun1-az3"),
    "ap-northeast-1": ("apne1-az1", "apne1-az2", "apne1-az4"),
    "ap-southeast-1": ("apse1-az1", "apse1-az2", "apse1-az3"),
    "ap-southeast-2": ("apse2-az1", "apse2-az2", "apse2-az3"),
    "ap-south-1": ("aps1-az1", "aps1-az2", "aps1-az3"),
}
AGENTS_CIDR = "10.20.0.0/16"
# Names the agents may resolve. Everything else gets NXDOMAIN.
ALLOWED_DOMAINS = ["amazonaws.com", "*.amazonaws.com"]


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


class AgentNetwork(Construct):
    """Private subnets with one NAT gateway, an HTTPS-only security group and a DNS allow-list."""

    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        region = Stack.of(self).region
        if region not in AGENTCORE_AZ_IDS:
            raise ValueError(
                f"AgentCore VPC mode lists no Availability Zones for {region}; "
                "deploy in a listed region or pass -c agentEgress=public"
            )
        flow_logs = logs.LogGroup(
            self,
            "FlowLogs",
            log_group_name=f"/{prefix}/agents-flow-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # One public subnet for the NAT gateway; the private subnets are added by AZ ID below.
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{prefix}-agents",
            ip_addresses=ec2.IpAddresses.cidr(AGENTS_CIDR),
            max_azs=1,
            nat_gateways=1,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="nat", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
            flow_logs={
                "all": ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs),
                    traffic_type=ec2.FlowLogTrafficType.ALL,
                )
            },
        )
        nat = self.vpc.public_subnets[0].node.find_child("NATGateway")
        assert isinstance(nat, ec2.CfnNatGateway)
        self.subnet_ids: list[str] = []
        route_tables: list[str] = []
        for i, az_id in enumerate(AGENTCORE_AZ_IDS[region][:2]):
            subnet = ec2.CfnSubnet(
                self,
                f"Private{i + 1}",
                vpc_id=self.vpc.vpc_id,
                availability_zone_id=az_id,
                cidr_block=f"10.20.{128 + i}.0/24",
                map_public_ip_on_launch=False,
                tags=[{"key": "Name", "value": f"{prefix}-agents-private-{az_id}"}],
            )
            table = ec2.CfnRouteTable(self, f"PrivateRoutes{i + 1}", vpc_id=self.vpc.vpc_id)
            ec2.CfnRoute(
                self,
                f"PrivateDefault{i + 1}",
                route_table_id=table.ref,
                destination_cidr_block="0.0.0.0/0",
                nat_gateway_id=nat.ref,
            )
            ec2.CfnSubnetRouteTableAssociation(
                self, f"PrivateAssoc{i + 1}", subnet_id=subnet.ref, route_table_id=table.ref
            )
            self.subnet_ids.append(subnet.ref)
            route_tables.append(table.ref)
        ec2.CfnVPCEndpoint(
            self,
            "S3Gateway",
            vpc_id=self.vpc.vpc_id,
            service_name=f"com.amazonaws.{region}.s3",
            vpc_endpoint_type="Gateway",
            route_table_ids=route_tables,
        )
        self.security_group = ec2.SecurityGroup(
            self,
            "AgentsSg",
            vpc=self.vpc,
            security_group_name=f"{prefix}-agents",
            description="AgentCore runtimes: HTTPS out only, nothing in",
            allow_all_outbound=False,
        )
        self.security_group.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to allow-listed names"
        )

        # DNS Firewall: allow the list, answer NXDOMAIN for everything else.
        self.allowed = resolver.CfnFirewallDomainList(
            self, "Allowed", name=f"{prefix}-agents-allowed", domains=list(ALLOWED_DOMAINS)
        )
        everything = resolver.CfnFirewallDomainList(
            self, "Everything", name=f"{prefix}-agents-everything", domains=["*"]
        )
        rules = resolver.CfnFirewallRuleGroup(
            self,
            "Rules",
            name=f"{prefix}-agents-egress",
            firewall_rules=[
                resolver.CfnFirewallRuleGroup.FirewallRuleProperty(
                    action="ALLOW", priority=100, firewall_domain_list_id=self.allowed.attr_id
                ),
                resolver.CfnFirewallRuleGroup.FirewallRuleProperty(
                    action="BLOCK",
                    priority=200,
                    firewall_domain_list_id=everything.attr_id,
                    block_response="NXDOMAIN",
                ),
            ],
        )
        resolver.CfnFirewallRuleGroupAssociation(
            self,
            "RulesAssociation",
            name=f"{prefix}-agents-egress",
            firewall_rule_group_id=rules.attr_id,
            vpc_id=self.vpc.vpc_id,
            priority=101,
            mutation_protection="DISABLED",
        )

    def allow(self, domain: str) -> None:
        """Add one name (a token is fine) to the allow-list."""
        self.allowed.domains = [*ALLOWED_DOMAINS, domain]

    def network_configuration(self):
        from aws_cdk import aws_bedrockagentcore as ac

        return ac.CfnRuntime.NetworkConfigurationProperty(
            network_mode="VPC",
            network_mode_config=ac.CfnRuntime.VpcConfigProperty(
                subnets=self.subnet_ids, security_groups=[self.security_group.security_group_id]
            ),
        )
