"""Review before deploy: synthesise the platform in cohort, solo and staged form and check what a
failed deploy would otherwise teach us one rollback at a time. Every fixture runs `app.py`
directly (no CDK CLI needed) with cdk-nag on."""

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
os.environ.setdefault("CDK_DEFAULT_ACCOUNT", "123456789012")
os.environ.setdefault("CDK_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION", "1")
CONNECTION = (
    "arn:aws:codeconnections:us-east-1:123456789012:connection/11111111-2222-3333-4444-555555555555"
)
TENANTS = ["alice", "bob"]
OWNERS = [*TENANTS, "live"]


def synth(name: str, context: dict) -> dict:
    out = HERE / "cdk.out.test" / name
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(
        [sys.executable, str(HERE / "app.py")],
        cwd=HERE,
        check=True,
        capture_output=True,
        env={**os.environ, "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(out)},
    )
    template = next(out.glob("northwind-*.template.json"))
    return json.loads(template.read_text())


@pytest.fixture(scope="module")
def cohort():
    return synth(
        "cohort",
        {
            "tenants": ",".join(TENANTS),
            "alertEmail": "ops@example.com",
            "budgetUsd": 300,
            "connectionArn": CONNECTION,
        },
    )


@pytest.fixture(scope="module")
def solo():
    return synth("solo", {"mode": "solo"})


@pytest.fixture(scope="module")
def staged():
    return synth(
        "staged",
        {
            "tenants": "alice",
            "env": "staging",
            "connectionArn": CONNECTION,
            "lakeFormation": "true",
        },
    )


def resources(t, kind):
    return {k: v for k, v in t["Resources"].items() if v["Type"] == kind}


def props(t, kind):
    return [v["Properties"] for v in resources(t, kind).values()]


def statements(t):
    for res in t["Resources"].values():
        if res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::Role"):
            docs = [res["Properties"].get("PolicyDocument")] + [
                p.get("PolicyDocument") for p in res["Properties"].get("Policies", [])
            ]
            for d in docs:
                if d:
                    yield from d["Statement"]


def text(value) -> str:
    return json.dumps(value)


# ----- data and governance ---------------------------------------------------------------


def test_buckets_are_kms_versioned_ssl_only_and_access_logged(cohort):
    buckets = props(cohort, "AWS::S3::Bucket")
    names = {b["BucketName"] for b in buckets}
    assert {
        "northwind-data-123456789012-us-east-1",
        "northwind-artifacts-123456789012-us-east-1",
    } <= names
    for b in buckets:
        if b["BucketName"].startswith("northwind-logs-"):
            continue
        sse = b["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"
        ]
        assert sse["SSEAlgorithm"] == "aws:kms", b["BucketName"]
        assert b["LoggingConfiguration"]["DestinationBucketName"]
        if any(k in b["BucketName"] for k in ("-data-", "-artifacts-", "-pipeline-")):
            assert b["VersioningConfiguration"]["Status"] == "Enabled"
    assert len(resources(cohort, "AWS::S3::BucketPolicy")) == len(buckets)
    key = next(iter(props(cohort, "AWS::KMS::Key")))
    assert key["EnableKeyRotation"] is True


def test_glue_catalog_has_the_tickets_table_and_lake_formation_is_opt_in(cohort, staged):
    table = next(iter(props(cohort, "AWS::Glue::Table")))["TableInput"]
    columns = {c["Name"] for c in table["StorageDescriptor"]["Columns"]}
    assert {"ticket_id", "subject", "body", "priority", "queue", "tags"} <= columns
    assert "JsonSerDe" in table["StorageDescriptor"]["SerdeInfo"]["SerializationLibrary"]
    assert (
        next(iter(props(cohort, "AWS::Glue::Database")))["DatabaseInput"]["Name"]
        == "northwind_support"
    )
    assert not resources(cohort, "AWS::LakeFormation::Resource")
    lf = next(iter(props(staged, "AWS::LakeFormation::Resource")))
    assert lf["UseServiceLinkedRole"] is True and lf["HybridAccessEnabled"] is True


# ----- tracking and registry -------------------------------------------------------------


def test_domain_mlflow_and_per_tenant_profiles_groups_and_roles(cohort):
    domain = next(iter(props(cohort, "AWS::SageMaker::Domain")))
    assert domain["AuthMode"] == "IAM" and domain["DomainName"] == "northwind-platform"
    assert domain["AppNetworkAccessType"] == "PublicInternetOnly" and len(domain["SubnetIds"]) == 2
    mlflow = next(iter(props(cohort, "AWS::SageMaker::MlflowTrackingServer")))
    assert (
        mlflow["TrackingServerName"] == "northwind-mlflow"
        and mlflow["TrackingServerSize"] == "Small"
    )
    assert mlflow["AutomaticModelRegistration"] is False
    profiles = {p["UserProfileName"] for p in props(cohort, "AWS::SageMaker::UserProfile")}
    assert profiles == {f"northwind-{t}" for t in TENANTS}
    groups = {
        g["ModelPackageGroupName"] for g in props(cohort, "AWS::SageMaker::ModelPackageGroup")
    }
    assert groups == {f"northwind-{t}-{p}" for t in TENANTS for p in ("triage", "semantic")}
    roles = {r.get("RoleName") for r in props(cohort, "AWS::IAM::Role")}
    assert {f"northwind-{t}-sagemaker" for t in TENANTS} <= roles
    assert "northwind-platform-sagemaker" in roles and "northwind-serving" in roles


def test_tenant_roles_stay_inside_their_prefix(cohort):
    """alice's execution role names only alice's resources (and the shared data prefix)."""
    for role_id, role in resources(cohort, "AWS::IAM::Role").items():
        if role["Properties"].get("RoleName") != "northwind-alice-sagemaker":
            continue
        policies = [
            p
            for p in props(cohort, "AWS::IAM::Policy")
            if any(r.get("Ref") == role_id for r in p["Roles"])
        ]
        body = text([p["PolicyDocument"] for p in policies])
        assert "northwind-alice-" in body and "northwind-bob" not in body
        assert "tenants/alice/" in body and "tenants/bob" not in body
        assert "foundation-model" not in body, "a training role never invokes models"


# ----- pipelines --------------------------------------------------------------------------


def test_retraining_schedules_are_weekly_and_disabled(cohort):
    rules = [r for r in props(cohort, "AWS::Events::Rule") if "-retrain-" in r.get("Name", "")]
    assert {r["Name"] for r in rules} == {
        f"northwind-{t}-retrain-{p}" for t in TENANTS for p in ("triage", "semantic")
    }
    for r in rules:
        assert r["State"] == "DISABLED" and r["ScheduleExpression"].startswith("cron(")
        target = r["Targets"][0]
        pipeline = r["Name"].rsplit("-", 1)[-1]
        assert f"-{pipeline}" in text(target["Arn"]) and "pipeline/northwind-" in text(
            target["Arn"]
        )
        passed = {
            p["Name"]: p["Value"]
            for p in target["SageMakerPipelineParameters"]["PipelineParameterList"]
        }
        assert passed["Trigger"] == "schedule"
        assert f"baselines/{pipeline}_production.json" in text(passed["ProductionSummary"])


def _declared_sagemaker_parameters() -> dict[str, set[str]]:
    """The SageMaker parameter names `nw/pipelines/params.py` declares, per pipeline, read with
    `ast` because this virtualenv does not have the course package."""
    import ast

    tree = ast.parse((HERE.parents[1] / "nw" / "pipelines" / "params.py").read_text())
    groups: dict[str, list[ast.Call]] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            if isinstance(target, ast.Name) and target.id in ("COMMON", "TRIAGE", "SEMANTIC"):
                groups[target.id] = [
                    c
                    for c in ast.walk(node.value)
                    if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "Param"
                ]

    def names(calls: list[ast.Call]) -> set[str]:
        out = set()
        for call in calls:
            baked = any(
                k.arg == "sagemaker" and getattr(k.value, "value", True) is False
                for k in call.keywords
            )
            if not baked:
                name = call.args[0].value
                out.add("".join(part.capitalize() for part in name.split("_")))
        return out

    common = names(groups["COMMON"])
    return {
        "triage": common | names(groups["TRIAGE"]),
        "semantic": common | names(groups["SEMANTIC"]),
    }


def test_every_parameter_a_schedule_passes_is_declared(cohort):
    declared = _declared_sagemaker_parameters()
    assert "Trigger" in declared["triage"] and "Trigger" in declared["semantic"]
    for r in props(cohort, "AWS::Events::Rule"):
        if "-retrain-" not in r.get("Name", ""):
            continue
        pipeline = r["Name"].rsplit("-", 1)[-1]
        for target in r["Targets"]:
            passed = {
                p["Name"] for p in target["SageMakerPipelineParameters"]["PipelineParameterList"]
            }
            assert passed <= declared[pipeline], (r["Name"], passed - declared[pipeline])


def test_production_summaries_are_deployed_to_baselines(cohort):
    deployments = resources(cohort, "Custom::CDKBucketDeployment")
    keys = []
    for logical, res in deployments.items():
        if "Baselines" not in logical:
            continue
        assert res["Properties"].get("Prune") is False
        keys += [text(k) for k in res["Properties"]["SourceObjectKeys"]]
    assert len(keys) == 2, keys
    assert cohort["Outputs"]["BaselinesUri"]


# ----- serving ----------------------------------------------------------------------------


def test_approval_event_reaches_the_deployer_with_the_rollback_alarms(cohort):
    rule = next(
        r for r in props(cohort, "AWS::Events::Rule") if r.get("Name") == "northwind-model-approved"
    )
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.sagemaker"]
    assert pattern["detail-type"] == ["SageMaker Model Package State Change"]
    assert pattern["detail"]["ModelApprovalStatus"] == ["Approved"]
    assert pattern["detail"]["ModelPackageGroupName"] == [{"prefix": "northwind-"}]
    fn = next(
        f
        for f in props(cohort, "AWS::Lambda::Function")
        if f.get("FunctionName") == "northwind-deploy-on-approval"
    )
    env = fn["Environment"]["Variables"]
    assert "sagemaker-model-monitor-analyzer" in text(env["NW_MONITOR_IMAGE"])
    # Alarm names are Refs at synth, so the JSON the function reads is a Fn::Join here.
    rollback = text(env["NW_ROLLBACK_ALARMS"])
    assert '\\"triage\\"' in rollback and '\\"semantic\\"' in rollback
    names = {a["AlarmName"] for a in props(cohort, "AWS::CloudWatch::Alarm")}
    for project in ("triage", "semantic"):
        for suffix in ("5xx", "p95", "drift"):
            assert f"northwind-live-{project}-{suffix}" in names
    drift = next(
        a
        for a in props(cohort, "AWS::CloudWatch::Alarm")
        if a["AlarmName"] == "northwind-live-triage-drift"
    )
    assert drift["Namespace"] == "aws/sagemaker/Endpoints/data-metrics"
    assert drift["MetricName"].startswith("feature_baseline_drift_")


def test_policy_lambda_is_arm64_versioned_canaried_and_behind_cognito(cohort):
    fn = next(
        f
        for f in props(cohort, "AWS::Lambda::Function")
        if f.get("FunctionName") == "northwind-policy"
    )
    assert fn["PackageType"] == "Image" and fn["Architectures"] == ["arm64"]
    assert fn["TracingConfig"]["Mode"] == "Active"
    env = fn["Environment"]["Variables"]
    assert env["NW_TENANT"] == "live" and env["NW_ENVIRONMENT"] == "northwind"
    assert (
        env["NW_METRICS_FORMAT"] == "emf"
        and "NW_GATEWAY_URL" in env
        and "NW_KNOWLEDGE_BASE_ID" in env
    )
    assert "NW_API_KEY_SECRET_ARN" in env and "NW_API_KEY" not in env
    assert len(resources(cohort, "AWS::Lambda::Version")) == 1
    alias = next(iter(props(cohort, "AWS::Lambda::Alias")))
    assert alias["Name"] == "live"
    group = next(iter(props(cohort, "AWS::CodeDeploy::DeploymentGroup")))
    assert group["DeploymentGroupName"] == "northwind-policy"
    assert group["DeploymentConfigName"] == "CodeDeployDefault.LambdaCanary10Percent15Minutes"
    assert group["AutoRollbackConfiguration"]["Enabled"] is True
    assert (
        group["AlarmConfiguration"]["Enabled"] is True
        and len(group["AlarmConfiguration"]["Alarms"]) == 2
    )
    api = next(iter(props(cohort, "AWS::ApiGatewayV2::Api")))
    assert api["Name"] == "northwind-policy" and api["ProtocolType"] == "HTTP"
    authorizer = next(iter(props(cohort, "AWS::ApiGatewayV2::Authorizer")))
    assert authorizer["AuthorizerType"] == "JWT"
    routes = props(cohort, "AWS::ApiGatewayV2::Route")
    by_key = {r["RouteKey"]: r for r in routes}
    assert by_key["ANY /{proxy+}"]["AuthorizationType"] == "JWT"
    assert by_key["GET /healthz"]["AuthorizationType"] == "NONE"
    stage = next(iter(props(cohort, "AWS::ApiGatewayV2::Stage")))
    assert stage["StageName"] == "v1" and stage["AccessLogSettings"]["DestinationArn"]
    assert not resources(cohort, "AWS::Lambda::Url"), (
        "the policy service is entered through the API, not a function URL"
    )


# ----- prompts and retrieval ---------------------------------------------------------------


def test_prompts_match_the_repo_catalog_and_carry_the_hash(cohort):
    catalog = json.loads((HERE / "prompts" / "catalog.json").read_text())
    prompts = props(cohort, "AWS::Bedrock::Prompt")
    assert len(prompts) == len(catalog) == len(resources(cohort, "AWS::Bedrock::PromptVersion"))
    by_tag = {p["Tags"]["nw:prompt"]: p for p in prompts}
    for row in catalog:
        p = by_tag[row["name"]]
        assert p["Tags"]["nw:sha256_12"] == row["sha256_12"]
        assert p["Variants"][0]["TemplateConfiguration"]["Text"]["Text"] == row["text"]
        assert p["Variants"][0]["TemplateType"] == "TEXT"
        assert p["Name"].startswith("northwind-")


def test_one_knowledge_base_per_owner_on_its_own_s3_vectors_index(cohort):
    indexes = props(cohort, "AWS::S3Vectors::Index")
    assert {i["IndexName"] for i in indexes} == {f"{o}-policies" for o in OWNERS}
    for i in indexes:
        assert i["Dimension"] == 1024 and i["DistanceMetric"] == "cosine"
        assert set(i["MetadataConfiguration"]["NonFilterableMetadataKeys"]) == {
            "AMAZON_BEDROCK_TEXT",
            "AMAZON_BEDROCK_METADATA",
        }
    kbs = props(cohort, "AWS::Bedrock::KnowledgeBase")
    assert {k["Name"] for k in kbs} == {f"northwind-{o}-policies" for o in OWNERS}
    for k in kbs:
        assert k["StorageConfiguration"]["Type"] == "S3_VECTORS"
        assert "titan-embed-text-v2" in text(k["KnowledgeBaseConfiguration"])
    sources = props(cohort, "AWS::Bedrock::DataSource")
    prefixes = {
        s["DataSourceConfiguration"]["S3Configuration"]["InclusionPrefixes"][0] for s in sources
    }
    assert prefixes == {f"tenants/{o}/policies/" for o in OWNERS}
    assert all(s["DataDeletionPolicy"] == "DELETE" for s in sources)


# ----- agents ------------------------------------------------------------------------------


def test_agentcore_plane_and_registry(cohort):
    runtimes = {r["AgentRuntimeName"]: r for r in props(cohort, "AWS::BedrockAgentCore::Runtime")}
    assert set(runtimes) == {"northwind_tools", "northwind_live_resolver"}
    assert runtimes["northwind_tools"]["ProtocolConfiguration"] == "MCP"
    assert runtimes["northwind_live_resolver"]["ProtocolConfiguration"] == "HTTP"
    env = runtimes["northwind_live_resolver"]["EnvironmentVariables"]
    assert (
        env["NW_APP"] == "nw.agent.agentcore:app"
        and env["PORT"] == "8080"
        and env["NW_TENANT"] == "live"
    )
    assert "NW_MEMORY_ID" in env and "NW_GATEWAY_URL" in env and "NW_KNOWLEDGE_BASE_ID" in env
    gateway = next(iter(props(cohort, "AWS::BedrockAgentCore::Gateway")))
    assert gateway["Name"] == "northwind-tools" and gateway["AuthorizerType"] == "AWS_IAM"
    assert gateway["PolicyEngineConfiguration"]["Mode"] == "ENFORCE"
    target = next(iter(props(cohort, "AWS::BedrockAgentCore::GatewayTarget")))
    assert (
        target["Name"] == "northwind-tools" and "McpServer" in target["TargetConfiguration"]["Mcp"]
    )
    policies = {p["Name"]: p for p in props(cohort, "AWS::BedrockAgentCore::Policy")}
    assert set(policies) == {"AllowReadTools", "DenyEscalateUnlessApprover"}
    assert (
        "NorthwindApprovers"
        in policies["DenyEscalateUnlessApprover"]["Definition"]["Cedar"]["Statement"]
    )
    memories = {m["Name"] for m in props(cohort, "AWS::BedrockAgentCore::Memory")}
    assert memories == {f"northwind_{o}_memory" for o in OWNERS}
    assert (
        next(iter(props(cohort, "AWS::BedrockAgentCore::WorkloadIdentity")))["Name"]
        == "northwind-web"
    )
    provider = next(iter(props(cohort, "AWS::BedrockAgentCore::ApiKeyCredentialProvider")))
    assert "resolve:secretsmanager" in text(provider["ApiKey"]), (
        "the key is a dynamic reference, never a literal"
    )
    evaluator = next(iter(props(cohort, "AWS::BedrockAgentCore::Evaluator")))
    assert (
        evaluator["Level"] == "TRACE"
        and evaluator["EvaluatorConfig"]["LlmAsAJudge"]["RatingScale"]["Numerical"]
    )
    online = next(iter(props(cohort, "AWS::BedrockAgentCore::OnlineEvaluationConfig")))
    assert (
        online["ExecutionStatus"] == "DISABLED"
        and online["Rule"]["SamplingConfig"]["SamplingPercentage"] == 10
    )
    assert online["DataSourceConfig"]["CloudWatchLogs"]["ServiceNames"] == [
        "northwind_live_resolver.DEFAULT"
    ]
    registry = next(iter(props(cohort, "AWS::AgentRegistry::Registry")))
    assert registry["Name"] == "northwind-agents" and registry["AuthorizerType"] == "AWS_IAM"
    records = {r["Name"]: r for r in props(cohort, "AWS::AgentRegistry::RegistryRecord")}
    assert records["northwind-tools"]["RecordType"] == "MCP"
    assert records["northwind-live-resolver"]["RecordType"] == "AGENT"
    assert (
        records["northwind-live-resolver"]["Descriptors"]["A2aAgentCard"]["DataSchemaVersion"]
        == "0.3"
    )
    assert next(iter(props(cohort, "AWS::Bedrock::Guardrail")))["Name"] == "northwind-support"


def test_runtime_role_matches_the_documented_shape_and_names(cohort):
    roles = {r.get("RoleName"): r for r in props(cohort, "AWS::IAM::Role")}
    assert "NorthwindBedrockAgentCoreRuntime-us-east-1" in roles
    assert "NorthwindBedrockAgentCoreGateway-us-east-1" in roles
    assert "AgentCoreEvaluationRole-northwind" in roles
    trust = roles["NorthwindBedrockAgentCoreRuntime-us-east-1"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]
    assert trust["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
    assert "aws:SourceAccount" in text(trust["Condition"])


# ----- model gateway -------------------------------------------------------------------------


def test_model_gateway_profiles_config_and_database(cohort):
    profiles = {
        p["InferenceProfileName"]
        for p in props(cohort, "AWS::Bedrock::ApplicationInferenceProfile")
    }
    assert profiles == {
        f"northwind-{o}-{r}" for o in OWNERS for r in ("workhorse", "judge", "economy")
    }
    for p in props(cohort, "AWS::Bedrock::ApplicationInferenceProfile"):
        assert {t["Key"] for t in p["Tags"]} == {"nw:tenant", "nw:role"}
    task = next(iter(props(cohort, "AWS::ECS::TaskDefinition")))
    container = task["ContainerDefinitions"][0]
    assert container["Image"].startswith("ghcr.io/berriai/litellm")
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["LITELLM_CONFIG_BUCKET_OBJECT_KEY"] == "gateway/litellm.yaml"
    secrets = {s["Name"] for s in container["Secrets"]}
    assert secrets == {"LITELLM_MASTER_KEY", "DATABASE_USERNAME", "DATABASE_PASSWORD"}
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    config = next(
        v["Properties"]
        for k, v in resources(cohort, "Custom::CDKBucketDeployment").items()
        if "Gateway" in k
    )
    assert config["Prune"] is False, "the artifacts bucket holds more than the config"
    body = _asset_text(cohort, config["SourceObjectKeys"][0], "gateway/litellm.yaml")
    assert "model_name: alice/workhorse" in body and "model_name: bob/judge" in body
    assert (
        "  - model_name: workhorse\n" in body
        and "master_key: os.environ/LITELLM_MASTER_KEY" in body
    )
    assert body.count("model: bedrock/") == 9
    db = next(iter(props(cohort, "AWS::RDS::DBCluster")))
    assert db["ServerlessV2ScalingConfiguration"]["MinCapacity"] == 0
    assert db["StorageEncrypted"] is True and db["Port"] == 5433
    assert next(iter(props(cohort, "AWS::RDS::DBInstance")))["PubliclyAccessible"] is False
    alb = next(iter(props(cohort, "AWS::ElasticLoadBalancingV2::LoadBalancer")))
    attrs = {a["Key"]: a["Value"] for a in alb["LoadBalancerAttributes"]}
    assert attrs["access_logs.s3.enabled"] == "true"


# ----- delivery ------------------------------------------------------------------------------


def test_delivery_pipeline_is_v2_with_source_build_approve_deploy(cohort, solo):
    pipeline = next(iter(props(cohort, "AWS::CodePipeline::Pipeline")))
    assert pipeline["PipelineType"] == "V2" and pipeline["Name"] == "northwind-delivery"
    stages = [s["Name"] for s in pipeline["Stages"]]
    assert stages == ["Source", "Build", "Approve", "Deploy"]
    source = pipeline["Stages"][0]["Actions"][0]
    assert source["ActionTypeId"]["Provider"] == "CodeStarSourceConnection"
    assert source["Configuration"]["ConnectionArn"] == CONNECTION
    assert pipeline["Stages"][2]["Actions"][0]["ActionTypeId"]["Provider"] == "Manual"
    repos = {r["RepositoryName"] for r in props(cohort, "AWS::ECR::Repository")}
    assert repos == {"northwind-policy", "northwind-agent", "northwind-pipelines"}
    projects = {p["Name"]: p for p in props(cohort, "AWS::CodeBuild::Project")}
    assert projects["northwind-build"]["Environment"]["PrivilegedMode"] is True
    assert projects["northwind-build"]["Environment"]["Type"] == "ARM_CONTAINER"
    assert "deploy/aws/delivery/deploy.sh" in text(projects["northwind-deploy"]["Source"])
    assert "docker build --platform linux/arm64" in text(projects["northwind-build"]["Source"])
    # The training image: x86_64 for SageMaker Processing, in its own project, pushed by digest.
    pipelines_build = projects["northwind-build-pipelines"]
    assert pipelines_build["Environment"]["Type"] == "LINUX_CONTAINER"
    body = text(pipelines_build["Source"])
    assert "docker build --platform linux/amd64" in body and "APP=pipelines" in body
    assert "--extra pipelines" in body and "imageDigest" in body
    build_stage = [a["Name"] for a in pipeline["Stages"][1]["Actions"]]
    assert build_stage == ["Images", "PipelinesImage"]
    assert "RepoPipelines" in text(cohort["Outputs"]["PipelineImage"])
    assert ":latest" in text(cohort["Outputs"]["PipelineImage"])
    assert "PipelineImage" in solo["Outputs"]
    roles = {r.get("RoleName") for r in props(cohort, "AWS::IAM::Role")}
    assert "northwind-deployer" in roles
    # Without a connection: repositories and the deployer exist, the pipeline does not.
    assert not resources(solo, "AWS::CodePipeline::Pipeline")
    assert {r["RepositoryName"] for r in props(solo, "AWS::ECR::Repository")} == repos
    assert "northwind-deployer" in {r.get("RoleName") for r in props(solo, "AWS::IAM::Role")}


# ----- identity and observability ----------------------------------------------------------


def test_cognito_trail_dashboard_alarms_and_budget(cohort):
    pool = next(iter(props(cohort, "AWS::Cognito::UserPool")))
    assert pool["UserPoolName"] == "northwind-web" and pool["MfaConfiguration"] == "ON"
    assert pool["Policies"]["PasswordPolicy"]["MinimumLength"] == 12
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    trail = next(iter(props(cohort, "AWS::CloudTrail::Trail")))
    assert trail["TrailName"] == "northwind-platform" and trail["EnableLogFileValidation"] is True
    assert trail["KMSKeyId"] and trail["CloudWatchLogsLogGroupArn"]
    board = next(iter(props(cohort, "AWS::CloudWatch::Dashboard")))
    body = text(board["DashboardBody"])
    for needle in (
        "AWS/SageMaker",
        "AWS/Lambda",
        "AWS/ApplicationELB",
        "AWS/ECS",
        "AWS/RDS",
        "AWS/Bedrock",
        "Northwind",
        "DriftLevel",
        "CostUsd",
    ):
        assert needle in body, needle
    assert body.count("InputTokenCount") >= 9, "one series per tenant and role"
    budget = next(iter(props(cohort, "AWS::Budgets::Budget")))["Budget"]
    assert budget["BudgetLimit"]["Amount"] == 300
    topic = next(iter(props(cohort, "AWS::SNS::Topic")))
    assert topic["DisplayName"] == "northwind-alerts"
    alarm_actions = [a for a in props(cohort, "AWS::CloudWatch::Alarm") if a.get("AlarmActions")]
    assert len(alarm_actions) == len(props(cohort, "AWS::CloudWatch::Alarm")), (
        "every alarm pages the topic"
    )


# ----- modes, environments, models, secrets ------------------------------------------------


def test_solo_mode_has_exactly_one_tenant(solo):
    assert (
        solo["Outputs"]["Mode"]["Value"] == "solo" and solo["Outputs"]["Tenants"]["Value"] == "solo"
    )
    assert {p["UserProfileName"] for p in props(solo, "AWS::SageMaker::UserProfile")} == {
        "northwind-solo"
    }
    assert {k["Name"] for k in props(solo, "AWS::Bedrock::KnowledgeBase")} == {
        "northwind-solo-policies",
        "northwind-live-policies",
    }
    assert len(props(solo, "AWS::Bedrock::ApplicationInferenceProfile")) == 6


def test_an_environment_word_reaches_every_name(staged):
    body = text(staged)
    assert "northwind-staging-alice-triage" in body and "northwind_staging_tools" in body
    for kind, key in (
        ("AWS::SageMaker::Domain", "DomainName"),
        ("AWS::SageMaker::MlflowTrackingServer", "TrackingServerName"),
        ("AWS::Cognito::UserPool", "UserPoolName"),
        ("AWS::CodePipeline::Pipeline", "Name"),
        ("AWS::ECR::Repository", "RepositoryName"),
        ("AWS::BedrockAgentCore::Gateway", "Name"),
        ("AWS::AgentRegistry::Registry", "Name"),
    ):
        assert all(p[key].startswith("northwind-staging") for p in props(staged, kind)), kind
    assert staged["Outputs"]["Environment"]["Value"] == "northwind-staging"
    assert (HERE / "cdk.out.test" / "staged" / "northwind-staging-platform.template.json").exists()


def test_bad_context_is_refused_before_synth():
    with pytest.raises(subprocess.CalledProcessError):
        synth("bad-env", {"tenants": "alice", "env": "Prod-1"})
    with pytest.raises(subprocess.CalledProcessError):
        synth("bad-tenant", {"tenants": "Alice"})
    with pytest.raises(subprocess.CalledProcessError):
        synth("bad-live", {"tenants": "live"})


def test_model_access_is_scoped_to_the_course_models_and_profiles(cohort):
    invoking = 0
    for st in statements(cohort):
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        if not any(a.startswith("bedrock:InvokeModel") for a in actions):
            continue
        invoking += 1
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        for r in res:
            body = text(r)
            if "foundation-model/" in body:
                assert "foundation-model/*" not in body, body
                assert any(
                    m in body
                    for m in ("gpt-oss-120b", "claude-opus-5", "nova-micro", "titan-embed")
                ), body
    assert invoking >= 4, "runtime, gateway task, evaluation, knowledge base"


def test_no_secret_value_is_in_the_template(cohort):
    body = text(cohort)
    for secret in props(cohort, "AWS::SecretsManager::Secret"):
        assert "SecretString" not in secret
    assert '"NW_API_KEY"' not in body and 'LITELLM_MASTER_KEY", "Value"' not in body


def test_outputs_have_the_keys_the_scripts_read(cohort):
    keys = set(cohort["Outputs"])
    assert {
        "Environment",
        "Mode",
        "Tenants",
        "DataBucket",
        "ArtifactsBucket",
        "ApiKeySecretArn",
        "GatewayUrl",
        "GatewayMasterKeyArn",
        "LiveGatewayKeyArn",
        "UrlPolicy",
        "KnowledgeBases",
        "KnowledgeBaseLive",
        "AgentRuntimeArn",
        "AgentRuntimeId",
        "RuntimeRoleArn",
        "ServingRoleArn",
        "RegistryId",
        "MlflowName",
        "DomainId",
        "Pipeline",
        "PipelineImage",
        "BaselinesUri",
        "DeployerRoleArn",
        "UserPoolId",
        "Models",
    } <= keys, keys
    assert not any("GatewayService" in k for k in keys), (
        "the pattern's generated outputs are removed"
    )


def _asset_text(template: dict, key, member: str) -> str:
    """The content of one file inside a zipped asset of the cohort synth."""
    import zipfile

    name = key if isinstance(key, str) else text(key)
    digest = name.split(".zip")[0].rsplit('"', 1)[-1]
    folder = HERE / "cdk.out.test" / "cohort" / f"asset.{digest}"
    if folder.is_dir():
        return (folder / member).read_text()
    with zipfile.ZipFile(folder.with_suffix(".zip")) as z:
        return z.read(member).decode()


def _nag_findings(name: str) -> list[str]:
    out = HERE / "cdk.out.test" / name
    report = next(out.glob("AwsSolutions-*-NagReport.csv"))
    with report.open() as f:
        rows = list(csv.DictReader(f))
    assert rows, "empty nag report"
    return [
        f"{r['Rule ID']} {r['Resource ID']}: {r['Rule Info']}"
        for r in rows
        if r["Compliance"] == "Non-Compliant"
    ]


def test_cdk_nag_has_no_unsuppressed_findings(cohort, solo, staged):
    for name in ("cohort", "solo", "staged"):
        findings = _nag_findings(name)
        assert not findings, "\n".join(findings)


def test_every_suppression_in_nag_py_is_used(cohort):
    """A suppression nobody needs is a claim nobody checked: keep the list tight."""
    import re

    listed = set(
        re.findall(r'"id": "(AwsSolutions-[A-Z0-9]+)"', (HERE / "stacks" / "nag.py").read_text())
    )
    report = next((HERE / "cdk.out.test" / "cohort").glob("AwsSolutions-*-NagReport.csv"))
    with report.open() as f:
        used = {r["Rule ID"] for r in csv.DictReader(f) if r["Compliance"] == "Suppressed"}
    assert listed == used, (listed - used, used - listed)
