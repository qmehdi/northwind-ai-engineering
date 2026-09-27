"""The Reference stack on AWS: the Session path plus the AgentCore plane.

- A tools runtime: the course MCP server as an AgentCore Runtime (MCP protocol, arm64).
- An agent runtime: the resolver as an AgentCore Runtime (HTTP protocol, arm64).
- A Gateway with AWS_IAM inbound auth and a Cedar policy engine in ENFORCE mode:
  `escalate` is denied to every principal except the approvers role, so the
  approval gate is enforced by the platform, not only by the loop.
- A Bedrock Guardrail (prompt attack, PII masking, grounding) with a snapshot version.
- An S3 Vectors bucket and index for the policy chunks: the managed retriever.
- An Agent Registry with a record for the resolver.

Every IAM statement here comes from the developer guide pages recorded in the
instructor research notes; `tests/test_synth.py` pins them.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Fn, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as ac
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3vectors as s3v
from constructs import Construct

from stacks.common import MODEL_IDS, bedrock_invoke_policy, image
from stacks.session_path import SessionPath

CEDAR_POLICIES = {
    # Tools every caller may use.
    "AllowReadTools": """permit(principal, action, resource)
when { ["search_policies", "classify_urgency", "classify_semantic", "find_similar_tickets", "lookup_customer", "check_entitlement"].contains(action.id) };""",
    # Escalation only for the approvers role. The runtime's own role is not in it,
    # so an agent calling escalate through the gateway is denied by the platform.
    "DenyEscalateUnlessApprover": """forbid(principal, action, resource)
when { action.id == "escalate" }
unless { principal.hasTag("northwind:approver") && principal.getTag("northwind:approver") == "true" };""",
}


class ReferenceStack(Stack):
    def __init__(
        self, scope: Construct, id: str, *, budget_usd: float, alert_email: str | None, **kw
    ) -> None:
        super().__init__(scope, id, **kw)
        self.path = SessionPath(
            self, "Session", region=self.region, budget_usd=budget_usd, alert_email=alert_email
        )

        # ----- guardrail -------------------------------------------------------
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name="northwind-support",
            blocked_input_messaging="This request was blocked by Northwind's safety policy.",
            blocked_outputs_messaging="The response was blocked by Northwind's safety policy.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    # Argus measured that HIGH prompt-attack blocks about 3 percent of ordinary support
                    # prompts because tool results are machine-built; LOW is the calibrated setting.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="LOW", output_strength="NONE"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="MISCONDUCT", input_strength="LOW", output_strength="NONE"
                    ),
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="CREDIT_DEBIT_CARD_NUMBER", action="ANONYMIZE"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
                ]
            ),
            contextual_grounding_policy_config=bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="GROUNDING", threshold=0.6
                    )
                ]
            ),
        )
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="northwind-support v1",
        )

        # ----- S3 Vectors: the managed retriever ---------------------------------
        vector_bucket = s3v.CfnVectorBucket(
            self,
            "PolicyVectors",
            vector_bucket_name=f"northwind-policy-{self.account}-{self.region}",
        )
        s3v.CfnIndex(
            self,
            "PolicyIndex",
            vector_bucket_name=vector_bucket.vector_bucket_name,
            index_name="policy-chunks",
            data_type="float32",
            dimension=384,  # all-MiniLM-L6-v2
            distance_metric="cosine",
        )

        # ----- runtime execution role (devguide runtime-permissions, verbatim shape) -----
        runtime_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=f"NorthwindBedrockAgentCoreRuntime-{self.region}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                    },
                },
            ),
            description="AgentCore Runtime execution role for the Northwind runtimes",
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRImageAccess",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRTokenAccess", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:PutResourcePolicy"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/northwind*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="GetAgentAccessToken",
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/northwind*",
                ],
            )
        )
        runtime_role.add_to_policy(bedrock_invoke_policy(self))
        self.path.api_key.grant_read(runtime_role)
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ApplyGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyVectors",
                actions=[
                    "s3vectors:QueryVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:ListVectors",
                ],
                resources=[
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/{vector_bucket.vector_bucket_name}/index/*"
                ],
            )
        )

        # ----- runtimes: tools (MCP) and the resolver (HTTP), both arm64 -----------------
        tools_image = image(self, "agent", platform=ecr_assets.Platform.LINUX_ARM64)
        common_env = {
            "NW_TRACK": "aws",
            "NW_AWS_REGION": self.region,
            "NW_LOG_FORMAT": "json",
            "NW_GUARDRAIL_ID": guardrail.attr_guardrail_id,
            "NW_GUARDRAIL_VERSION": guardrail_version.attr_version,
            "NW_VECTOR_BUCKET": vector_bucket.vector_bucket_name,
            "NW_VECTOR_INDEX": "policy-chunks",
            "NW_API_KEY_SECRET_ARN": self.path.api_key.secret_arn,
            "NW_TRACE_EXPORT": "xray",
        }
        tools_runtime = ac.CfnRuntime(
            self,
            "ToolsRuntime",
            agent_runtime_name="northwind_tools",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=ac.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=ac.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=tools_image.image_uri
                )
            ),
            protocol_configuration="MCP",
            network_configuration=ac.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            environment_variables={
                **common_env,
                "NW_APP": "mcp",
                "NW_MCP_HOST": "0.0.0.0",
                "NW_MCP_PORT": "8000",
                "PORT": "8000",
            },
            description="Northwind tool registry as an MCP server",
        )
        agent_runtime = ac.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name="northwind_resolver",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=ac.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=ac.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=tools_image.image_uri
                )
            ),
            protocol_configuration="HTTP",
            network_configuration=ac.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            environment_variables={
                **common_env,
                "NW_APP": "nw.agent.service:app",
                "NW_AGENT_ROLE": "resolver",
                "PORT": "8080",
            },
            description="Northwind resolver agent",
        )
        for r in (tools_runtime, agent_runtime):
            r.node.add_dependency(runtime_role)

        # ----- policy engine and Cedar policies ------------------------------------------
        engine = ac.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name="northwind_tools",
            description="Authorises tool calls through the Northwind gateway",
        )
        for name, statement in CEDAR_POLICIES.items():
            ac.CfnPolicy(
                self,
                f"Policy{name}",
                name=name,
                policy_engine_id=engine.attr_policy_engine_id,
                definition=ac.CfnPolicy.PolicyDefinitionProperty(
                    cedar=ac.CfnPolicy.CedarPolicyProperty(statement=statement)
                ),
                enforcement_mode="ACTIVE",
                validation_mode="FAIL_ON_ANY_FINDINGS",
            )

        # ----- gateway role: exact set from the devguide policy-permissions page (Argus ADR-0013) ---
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="AgentCore Gateway execution role",
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeToolsRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    tools_runtime.attr_agent_runtime_arn,
                    f"{tools_runtime.attr_agent_runtime_arn}/*",
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineRead",
                actions=["bedrock-agentcore:GetPolicyEngine"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*"
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineAuthorize",
                actions=[
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                ],
            )
        )

        gateway = ac.CfnGateway(
            self,
            "Gateway",
            name="northwind-tools",
            role_arn=gateway_role.role_arn,
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            protocol_configuration=ac.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=ac.CfnGateway.MCPGatewayConfigurationProperty(
                    search_type="SEMANTIC", supported_versions=["2025-06-18"]
                )
            ),
            policy_engine_configuration=ac.CfnGateway.GatewayPolicyEngineConfigurationProperty(
                arn=engine.attr_policy_engine_arn, mode="ENFORCE"
            ),
            description="Northwind tools behind a Cedar policy",
        )
        gateway.node.add_dependency(gateway_role)

        # The MCP runtime's invocation URL: the ARN URL-encoded inside the runtimes path.
        encoded = Fn.join(
            "%2F",
            Fn.split("/", Fn.join("%3A", Fn.split(":", tools_runtime.attr_agent_runtime_arn))),
        )
        endpoint = f"https://bedrock-agentcore.{self.region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"
        ac.CfnGatewayTarget(
            self,
            "ToolsTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name="northwind-tools",
            description="The course tool registry",
            target_configuration=ac.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=ac.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=ac.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=endpoint
                    )
                )
            ),
            credential_provider_configurations=[
                ac.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )

        # ----- registry --------------------------------------------------------------
        logs.LogGroup(
            self,
            "RuntimeLogs",
            log_group_name="/aws/bedrock-agentcore/runtimes/northwind",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "AgentRuntimeArn", value=agent_runtime.attr_agent_runtime_arn)
        CfnOutput(self, "ToolsRuntimeArn", value=tools_runtime.attr_agent_runtime_arn)
        CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        CfnOutput(self, "Models", value=",".join(MODEL_IDS.values()))
