"""Agents: the AgentCore plane plus the registry, after the AgentOps reference.

- Runtime: the course MCP server as an AgentCore Runtime (MCP protocol) and the live resolver
  (HTTP protocol), both from the agent image on arm64. Tenants deploy their own resolver
  runtime through `nw.platform.AgentRuntime.deploy` (the API), named `northwind_<tenant>_resolver`,
  with the same execution role.
- Gateway: the MCP runtime as the target, AWS_IAM inbound, a Cedar policy engine in ENFORCE
  mode: read tools for every caller, `escalate` only for the approvers role.
- Memory: one AgentCore Memory per tenant and one for live, short-term events plus a semantic
  long-term strategy, 30 days.
- Identity: a workload identity for the web entry and an API key credential provider holding
  the service API key so the agent fetches it through Identity instead of an environment
  variable.
- Observability: one log group for the runtimes with a retention and the `drift_alert` metric.
- Evaluations: a custom LLM-as-a-judge evaluator (the course Judge model) at TRACE level and an
  online evaluation config over the runtime log group, disabled until the evaluation step
  enables it; the execution role is the one on the evaluations-prerequisites devguide page
  (fetched 2026-09-29).
- Registry: an AWS Agent Registry (IAM discovery, manual approval) with two records the
  stack owns: the tools MCP server and the live resolver agent. Tenants register their own
  agent card through `nw.platform.AgentRuntime.register`.
- Guardrail: prompt attack, PII masking, grounding, with a snapshot version.
"""

from __future__ import annotations

import json

from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_agentregistry as registry
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as ac
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secrets
from aws_cdk import aws_sns as sns
from constructs import Construct

from stacks.common import LIVE, MODEL_IDS, bedrock_invoke_policy, image

TARGET_NAME = "northwind-tools"
READ_TOOLS = (
    "search_policies",
    "classify_urgency",
    "classify_semantic",
    "find_similar_tickets",
    "lookup_customer",
    "check_entitlement",
)
APPROVERS_ROLE = "NorthwindApprovers"
JUDGE_INSTRUCTIONS = (
    "You grade one turn of a support agent for Northwind Cloud. Read the customer's request, "
    "the tool calls and the final answer. Score how helpful and grounded the answer is: it must "
    "answer the question, cite the policy it relied on when one applies, and never invent an "
    "entitlement, a refund or a date. A refusal on an unanswerable request scores 4 or 5."
)


def cedar_policies(account: str) -> dict[str, str]:
    actions = ", ".join(f'AgentCore::Action::"{TARGET_NAME}___{t}"' for t in READ_TOOLS)
    return {
        "AllowReadTools": (
            "permit(principal is AgentCore::IamEntity, "
            f"action in [{actions}], "
            "resource is AgentCore::Gateway);"
        ),
        "DenyEscalateUnlessApprover": (
            f'forbid(principal, action == AgentCore::Action::"{TARGET_NAME}___escalate", resource) '
            f'unless {{ principal.id like "arn:aws:sts::{account}:assumed-role/{APPROVERS_ROLE}/*" }};'
        ),
    }


class Agents(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        env_name: str,
        tenants: list[str],
        api_key: secrets.ISecret,
        gateway_url: str,
        gateway_key: secrets.ISecret,
        knowledge_base_id: str,
        topic: sns.ITopic,
        cognito_domain_url: str,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        under = prefix.replace("-", "_")
        camel = "Northwind" + env_name.capitalize()

        # ----- guardrail -----
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{prefix}-support",
            blocked_input_messaging="This request was blocked by Northwind's safety policy.",
            blocked_outputs_messaging="The response was blocked by Northwind's safety policy.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
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
            description=f"{prefix}-support v1",
        )

        # ----- runtime logs -----
        self.log_group = logs.LogGroup(
            self,
            "RuntimeLogs",
            log_group_name=f"/aws/bedrock-agentcore/runtimes/{prefix}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        drift = logs.MetricFilter(
            self,
            "DriftFilter",
            log_group=self.log_group,
            metric_namespace="Northwind",
            metric_name=f"DriftAlerts-{env_name}-agent" if env_name else "DriftAlerts-agent",
            filter_pattern=logs.FilterPattern.string_value("$.msg", "=", "drift_alert"),
            metric_value="1",
        ).metric(period=Duration.minutes(5), statistic="Sum")
        self.drift_alarm = cw.Alarm(
            self,
            "AlarmDrift",
            alarm_name=f"{prefix}-agent-drift",
            metric=drift,
            threshold=1,
            evaluation_periods=1,
            alarm_description="agent behaviour drift signal past its bar",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        self.drift_alarm.add_alarm_action(cw_actions.SnsAction(topic))

        # ----- memory: one per tenant and one for live -----
        self.memories: dict[str, ac.CfnMemory] = {}
        for owner in [*tenants, LIVE]:
            self.memories[owner] = ac.CfnMemory(
                self,
                f"Memory{owner.title()}",
                name=f"{under}_{owner}_memory",
                description=f"Resolver memory for {owner}",
                event_expiry_duration=30,
                memory_strategies=[
                    ac.CfnMemory.MemoryStrategyProperty(
                        semantic_memory_strategy=ac.CfnMemory.SemanticMemoryStrategyProperty(
                            name="facts", namespaces=[f"/{owner}/{{actorId}}/facts"]
                        )
                    )
                ],
                tags={"nw:tenant": owner},
            )

        # ----- identity -----
        self.workload_identity = ac.CfnWorkloadIdentity(
            self,
            "WebIdentity",
            name=f"{prefix}-web",
            allowed_resource_oauth2_return_urls=[f"{cognito_domain_url}/oauth2/idpresponse"],
        )
        self.api_key_provider = ac.CfnApiKeyCredentialProvider(
            self,
            "ApiKeyProvider",
            name=f"{prefix}-service-api-key",
            # A CloudFormation dynamic reference: the template carries the secret's ARN, the
            # value is resolved at deploy and stored in the AgentCore token vault.
            api_key=api_key.secret_value.unsafe_unwrap(),
        )

        # ----- runtime execution role (devguide runtime-permissions shape) -----
        runtime_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=f"{camel}BedrockAgentCoreRuntime-{stack.region}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:*"
                    },
                },
            ),
            description="AgentCore Runtime execution role for the Northwind runtimes",
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRImageAccess",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[f"arn:aws:ecr:{stack.region}:{stack.account}:repository/*"],
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
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:PutResourcePolicy"],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/runtimes/{prefix}*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{stack.region}:{stack.account}:log-group:*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
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
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:workload-identity-directory/default/workload-identity/{under}*",
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ResourceCredentials",
                actions=["bedrock-agentcore:GetResourceApiKey"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:token-vault/default",
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:token-vault/default/apikeycredentialprovider/{prefix}-service-api-key",
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="Memory",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                resources=[m.attr_memory_arn for m in self.memories.values()],
            )
        )
        runtime_role.add_to_policy(bedrock_invoke_policy(self))
        api_key.grant_read(runtime_role)
        gateway_key.grant_read(runtime_role)
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ApplyGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="RetrievePolicies",
                actions=["bedrock:Retrieve"],
                resources=[f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeTenantEndpoints",
                actions=["sagemaker:InvokeEndpoint"],
                resources=[f"arn:aws:sagemaker:{stack.region}:{stack.account}:endpoint/{prefix}-*"],
            )
        )
        self.runtime_role = runtime_role

        # ----- runtimes -----
        agent_image = image(self, "agent", platform=ecr_assets.Platform.LINUX_ARM64)
        self.agent_image = agent_image
        common_env = {
            "NW_TRACK": "aws",
            "NW_AWS_REGION": stack.region,
            "NW_ENVIRONMENT": prefix,
            "NW_TENANT": LIVE,
            "NW_LOG_FORMAT": "json",
            "NW_GUARDRAIL_ID": guardrail.attr_guardrail_id,
            "NW_GUARDRAIL_VERSION": guardrail_version.attr_version,
            "NW_KNOWLEDGE_BASE_ID": knowledge_base_id,
            "NW_API_KEY_SECRET_ARN": api_key.secret_arn,
            "NW_GATEWAY_URL": gateway_url,
            "NW_GATEWAY_KEY_SECRET_ARN": gateway_key.secret_arn,
            "NW_MEMORY_ID": self.memories[LIVE].attr_memory_id,
            "NW_TRACE_EXPORT": "xray",
            "NW_STAGE": env_name,
        }
        self.tools_runtime = ac.CfnRuntime(
            self,
            "ToolsRuntime",
            agent_runtime_name=f"{under}_tools",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=ac.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=ac.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=agent_image.image_uri
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
        self.agent_runtime = ac.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name=f"{under}_{LIVE}_resolver",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=ac.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=ac.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=agent_image.image_uri
                )
            ),
            protocol_configuration="HTTP",
            network_configuration=ac.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            environment_variables={
                **common_env,
                "NW_APP": "nw.agent.agentcore:app",
                "NW_AGENT_ROLE": "resolver",
                "NW_SPEND_CAP_USD": "25",
                "PORT": "8080",
            },
            description="Northwind resolver agent, the promoted target (HTTP protocol, nw.agent.agentcore)",
        )
        for r in (self.tools_runtime, self.agent_runtime):
            r.node.add_dependency(runtime_role)

        # ----- policy engine, gateway, target -----
        engine = ac.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name=f"{under}_tools",
            description="Authorises tool calls through the Northwind gateway",
        )
        for name, statement in cedar_policies(stack.account).items():
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
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            role_name=f"{camel}BedrockAgentCoreGateway-{stack.region}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:*"
                    },
                },
            ),
            description="AgentCore Gateway execution role",
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeToolsRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    self.tools_runtime.attr_agent_runtime_arn,
                    f"{self.tools_runtime.attr_agent_runtime_arn}/*",
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineRead",
                actions=["bedrock-agentcore:GetPolicyEngine"],
                resources=[engine.attr_policy_engine_arn],
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
                    engine.attr_policy_engine_arn,
                    f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/*",
                ],
            )
        )
        self.gateway = ac.CfnGateway(
            self,
            "Gateway",
            name=f"{prefix}-tools",
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
        self.gateway.node.add_dependency(gateway_role)
        encoded = Fn.join(
            "%2F",
            Fn.split("/", Fn.join("%3A", Fn.split(":", self.tools_runtime.attr_agent_runtime_arn))),
        )
        tools_endpoint = f"https://bedrock-agentcore.{stack.region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"
        ac.CfnGatewayTarget(
            self,
            "ToolsTarget",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=TARGET_NAME,
            description="The course tool registry",
            target_configuration=ac.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=ac.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=ac.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=tools_endpoint
                    )
                )
            ),
            credential_provider_configurations=[
                ac.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )

        # ----- evaluations -----
        eval_role = iam.Role(
            self,
            "EvaluationRole",
            role_name=f"AgentCoreEvaluationRole-{prefix}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": stack.account,
                        "aws:ResourceAccount": stack.account,
                    },
                    "ArnLike": {
                        "aws:SourceArn": [
                            f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:evaluator/*",
                            f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:online-evaluation-config/*",
                        ]
                    },
                },
            ),
            description="AgentCore Evaluations execution role (devguide evaluations-prerequisites)",
        )
        eval_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogReadStatement",
                actions=["logs:DescribeLogGroups", "logs:GetQueryResults", "logs:StartQuery"],
                resources=["*"],
            )
        )
        eval_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogWriteStatement",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/evaluations/*"
                ],
            )
        )
        eval_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchIndexPolicyStatement",
                actions=["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:aws/spans",
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:aws/spans:*",
                ],
            )
        )
        eval_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeStatement",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{stack.region}::foundation-model/{MODEL_IDS['judge']}"
                ],
            )
        )
        self.evaluator = ac.CfnEvaluator(
            self,
            "Evaluator",
            evaluator_name=f"{under}_helpfulness",
            description="Course judge: helpful and grounded, 1 to 5",
            level="TRACE",
            evaluator_config=ac.CfnEvaluator.EvaluatorConfigProperty(
                llm_as_a_judge=ac.CfnEvaluator.LlmAsAJudgeEvaluatorConfigProperty(
                    instructions=JUDGE_INSTRUCTIONS,
                    model_config=ac.CfnEvaluator.EvaluatorModelConfigProperty(
                        bedrock_evaluator_model_config=ac.CfnEvaluator.BedrockEvaluatorModelConfigProperty(
                            model_id=MODEL_IDS["judge"]
                        )
                    ),
                    rating_scale=ac.CfnEvaluator.RatingScaleProperty(
                        numerical=[
                            ac.CfnEvaluator.NumericalScaleDefinitionProperty(
                                value=1,
                                label="harmful",
                                definition="Wrong or invented facts, or an unsafe action",
                            ),
                            ac.CfnEvaluator.NumericalScaleDefinitionProperty(
                                value=2, label="unhelpful", definition="Does not answer the request"
                            ),
                            ac.CfnEvaluator.NumericalScaleDefinitionProperty(
                                value=3,
                                label="partial",
                                definition="Answers without citing the policy it relies on",
                            ),
                            ac.CfnEvaluator.NumericalScaleDefinitionProperty(
                                value=4,
                                label="good",
                                definition="Correct, grounded, cites the policy",
                            ),
                            ac.CfnEvaluator.NumericalScaleDefinitionProperty(
                                value=5,
                                label="excellent",
                                definition="Correct, grounded, concise, next step stated",
                            ),
                        ]
                    ),
                )
            ),
        )
        self.online_evaluation = ac.CfnOnlineEvaluationConfig(
            self,
            "OnlineEvaluation",
            online_evaluation_config_name=f"{under}_online",
            description="10 percent of live resolver sessions, judged by the course evaluator",
            evaluation_execution_role_arn=eval_role.role_arn,
            data_source_config=ac.CfnOnlineEvaluationConfig.DataSourceConfigProperty(
                cloud_watch_logs=ac.CfnOnlineEvaluationConfig.CloudWatchLogsInputConfigProperty(
                    log_group_names=[self.log_group.log_group_name],
                    service_names=[f"{under}_{LIVE}_resolver.DEFAULT"],
                )
            ),
            evaluators=[
                ac.CfnOnlineEvaluationConfig.EvaluatorReferenceProperty(
                    evaluator_id=self.evaluator.attr_evaluator_id
                ),
                ac.CfnOnlineEvaluationConfig.EvaluatorReferenceProperty(
                    evaluator_id="Builtin.Helpfulness"
                ),
            ],
            rule=ac.CfnOnlineEvaluationConfig.RuleProperty(
                sampling_config=ac.CfnOnlineEvaluationConfig.SamplingConfigProperty(
                    sampling_percentage=10
                )
            ),
            execution_status="DISABLED",
        )
        self.online_evaluation.node.add_dependency(eval_role)

        # ----- the agent registry -----
        self.registry = registry.CfnRegistry(
            self,
            "Registry",
            name=f"{prefix}-agents",
            description="Northwind agents, tools and skills that are approved for use",
            authorizer_type="AWS_IAM",
            approval_configuration=registry.CfnRegistry.ApprovalConfigurationProperty(
                auto_approval_rules=[]
            ),
        )
        tools_card = {
            "name": f"io.northwind/{prefix}-tools",
            "description": "Northwind support tools as an MCP server behind the AgentCore gateway",
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": self.gateway.attr_gateway_url}],
        }
        self.tools_record = registry.CfnRegistryRecord(
            self,
            "ToolsRecord",
            registry_id=self.registry.attr_registry_id,
            name=f"{prefix}-tools",
            display_name="Northwind tools",
            description="search_policies, classify_urgency, classify_semantic, find_similar_tickets, lookup_customer, check_entitlement, escalate",
            record_type="MCP",
            record_version="1.0.0",
            descriptors=registry.CfnRegistryRecord.DescriptorsProperty(
                mcp_server=registry.CfnRegistryRecord.McpServerDescriptorProperty(
                    data=Stack.of(self).to_json_string(tools_card), data_schema_version="2025-12-11"
                )
            ),
        )
        agent_card = {
            "name": f"{prefix}-resolver",
            "description": "Northwind support resolver: triage first, then the cheapest loop that will do",
            "url": f"https://bedrock-agentcore.{stack.region}.amazonaws.com/runtimes/{encoded}/invocations",
            "version": "1.0.0",
            "protocolVersion": "0.3",
            "capabilities": {"streaming": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "route",
                    "name": "Route a ticket",
                    "description": "Classify and resolve a support ticket",
                    "tags": ["support"],
                }
            ],
        }
        self.agent_record = registry.CfnRegistryRecord(
            self,
            "ResolverRecord",
            registry_id=self.registry.attr_registry_id,
            name=f"{prefix}-{LIVE}-resolver",
            display_name="Northwind resolver (live)",
            description="The promoted resolver agent on AgentCore Runtime",
            record_type="AGENT",
            record_version="1.0.0",
            descriptors=registry.CfnRegistryRecord.DescriptorsProperty(
                a2_a_agent_card=registry.CfnRegistryRecord.A2aAgentCardDescriptorProperty(
                    data=Stack.of(self).to_json_string(agent_card), data_schema_version="0.3"
                )
            ),
        )

        CfnOutput(
            self, "OutGatewayToolsUrl", value=self.gateway.attr_gateway_url
        ).override_logical_id("GatewayToolsUrl")
        CfnOutput(
            self, "OutAgentRuntimeArn", value=self.agent_runtime.attr_agent_runtime_arn
        ).override_logical_id("AgentRuntimeArn")
        CfnOutput(
            self, "OutAgentRuntimeId", value=self.agent_runtime.attr_agent_runtime_id
        ).override_logical_id("AgentRuntimeId")
        CfnOutput(
            self, "OutToolsRuntimeArn", value=self.tools_runtime.attr_agent_runtime_arn
        ).override_logical_id("ToolsRuntimeArn")
        CfnOutput(self, "OutRuntimeRoleArn", value=runtime_role.role_arn).override_logical_id(
            "RuntimeRoleArn"
        )
        CfnOutput(self, "OutGuardrailId", value=guardrail.attr_guardrail_id).override_logical_id(
            "GuardrailId"
        )
        CfnOutput(self, "OutRegistryId", value=self.registry.attr_registry_id).override_logical_id(
            "RegistryId"
        )
        CfnOutput(self, "OutAgentImage", value=agent_image.image_uri).override_logical_id(
            "AgentImage"
        )
        CfnOutput(
            self,
            "OutMemories",
            value=",".join(f"{o}={m.attr_memory_id}" for o, m in self.memories.items()),
        ).override_logical_id("Memories")
        self.cedar = json.dumps(cedar_policies(stack.account))
