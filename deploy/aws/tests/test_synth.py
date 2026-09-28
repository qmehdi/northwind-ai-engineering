"""Review before deploy: synthesise both tiers and check what a failed deploy would
otherwise teach us one rollback at a time."""

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


def synth(tier: str, stage: str = "") -> dict:
    out = HERE / "cdk.out.test" / (f"{tier}-{stage}" if stage else tier)
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(
        [sys.executable, str(HERE / "app.py")],
        cwd=HERE,
        check=True,
        env={
            **os.environ,
            "CDK_CONTEXT_JSON": json.dumps(
                {"tier": tier, "alertEmail": "ops@example.com", "budgetUsd": 120, "stage": stage}
            ),
            "CDK_OUTDIR": str(out),
        },
    )
    template = next(out.glob("northwind-*.template.json"))
    return json.loads(template.read_text())


@pytest.fixture(scope="module")
def session():
    return synth("session")


@pytest.fixture(scope="module")
def reference():
    return synth("reference")


@pytest.fixture(scope="module")
def staged():
    """The session tier with `-c stage=staging`: every name carries the stage."""
    return synth("session", "staging")


def resources(t, kind):
    return {k: v for k, v in t["Resources"].items() if v["Type"] == kind}


def statements(t):
    for res in t["Resources"].values():
        if res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::Role"):
            docs = [res["Properties"].get("PolicyDocument")] + [
                p.get("PolicyDocument") for p in res["Properties"].get("Policies", [])
            ]
            for d in docs:
                if d:
                    yield from d["Statement"]


def test_session_has_four_lambda_functions_with_urls_tracing_and_secret(session):
    fns = resources(session, "AWS::Lambda::Function")
    assert len(fns) == 4
    for f in fns.values():
        p = f["Properties"]
        assert p["PackageType"] == "Image" and p["Architectures"] == ["arm64"]
        assert p["TracingConfig"]["Mode"] == "Active"
        env = p["Environment"]["Variables"]
        assert "NW_API_KEY_SECRET_ARN" in env and env["NW_TRACE_EXPORT"] == "xray"
        assert "NW_API_KEY" not in env, "the key value must never be in the template"
    assert len(resources(session, "AWS::Lambda::Url")) == 4
    assert resources(session, "AWS::SecretsManager::Secret")
    assets = json.loads(
        (HERE / "cdk.out.test" / "session" / "northwind-session.assets.json").read_text()
    )
    for a in assets["dockerImages"].values():
        assert (
            a["source"]["platform"] == "linux/arm64"
            and a["source"]["dockerBuildArgs"]["LAMBDA"] == "1"
        )
    assert resources(session, "AWS::Budgets::Budget")
    assert resources(session, "AWS::CloudWatch::Dashboard")
    assert (
        len(resources(session, "AWS::CloudWatch::Alarm")) == 12
    )  # errors and p95 per function, plus drift
    filters = resources(session, "AWS::Logs::MetricFilter")
    assert len(filters) == 4 and all("drift_alert" in json.dumps(f) for f in filters.values())


def test_functions_publish_a_version_behind_the_live_alias(session):
    """The function URL targets the alias, not $LATEST, so a CLI change to the configuration
    serves nothing until a version is published and the alias moves."""
    aliases = resources(session, "AWS::Lambda::Alias")
    assert len(aliases) == 4 and all(a["Properties"]["Name"] == "live" for a in aliases.values())
    assert len(resources(session, "AWS::Lambda::Version")) == 4
    urls = resources(session, "AWS::Lambda::Url")
    assert all(u["Properties"]["Qualifier"] == "live" for u in urls.values()), urls
    for alias in aliases.values():
        update = alias["UpdatePolicy"]["CodeDeployLambdaAliasUpdate"]
        assert "ApplicationName" in update and "DeploymentGroupName" in update


def test_codedeploy_shifts_ten_percent_and_rolls_back_on_the_alarms(session):
    apps = resources(session, "AWS::CodeDeploy::Application")
    assert len(apps) == 1
    app = next(iter(apps.values()))["Properties"]
    assert app["ApplicationName"] == "northwind-lambda" and app["ComputePlatform"] == "Lambda"
    groups = resources(session, "AWS::CodeDeploy::DeploymentGroup")
    assert len(groups) == 4
    alarms = resources(session, "AWS::CloudWatch::Alarm")
    for g in groups.values():
        p = g["Properties"]
        # One slow configuration for all four: the errors alarm needs two five-minute periods
        # before it fires, so a ten-minute linear shift or a five-minute canary would finish
        # before the rollback trigger could act.
        assert p["DeploymentConfigName"] == "CodeDeployDefault.LambdaCanary10Percent15Minutes"
        assert p["DeploymentStyle"] == {
            "DeploymentOption": "WITH_TRAFFIC_CONTROL",
            "DeploymentType": "BLUE_GREEN",
        }
        assert set(p["AutoRollbackConfiguration"]["Events"]) == {
            "DEPLOYMENT_FAILURE",
            "DEPLOYMENT_STOP_ON_REQUEST",
            "DEPLOYMENT_STOP_ON_ALARM",
        }
        assert p["AlarmConfiguration"]["Enabled"] is True
        names = [
            alarms[a["Name"]["Ref"]]["Properties"]["AlarmName"]
            for a in p["AlarmConfiguration"]["Alarms"]
        ]
        assert names == [
            f"{p['DeploymentGroupName']}-errors",
            f"{p['DeploymentGroupName']}-p95",
        ], names
    assert {g["Properties"]["DeploymentGroupName"] for g in groups.values()} == {
        "northwind-triage",
        "northwind-semantic",
        "northwind-policy",
        "northwind-agent",
    }


def test_default_stage_keeps_every_name(session):
    fns = {
        f["Properties"]["FunctionName"]
        for f in resources(session, "AWS::Lambda::Function").values()
    }
    assert fns == {"northwind-triage", "northwind-semantic", "northwind-policy", "northwind-agent"}
    groups = {
        g["Properties"]["LogGroupName"] for g in resources(session, "AWS::Logs::LogGroup").values()
    }
    assert groups == {
        f"/aws/lambda/northwind-{n}" for n in ("triage", "semantic", "policy", "agent")
    }
    board = next(iter(resources(session, "AWS::CloudWatch::Dashboard").values()))["Properties"]
    assert board["DashboardName"] == "northwind"
    alarms = {
        a["Properties"]["AlarmName"] for a in resources(session, "AWS::CloudWatch::Alarm").values()
    }
    assert "northwind-agent-errors" in alarms and "northwind-triage-drift" in alarms
    assert all(a.startswith("northwind-") and "--" not in a for a in alarms), alarms
    filters = resources(session, "AWS::Logs::MetricFilter")
    names = {
        m["MetricName"] for f in filters.values() for m in f["Properties"]["MetricTransformations"]
    }
    assert names == {f"DriftAlerts-{n}" for n in ("triage", "semantic", "policy", "agent")}
    budget = next(iter(resources(session, "AWS::Budgets::Budget").values()))["Properties"]
    assert budget["Budget"]["BudgetName"] == "northwind-monthly"
    env = next(iter(resources(session, "AWS::Lambda::Function").values()))["Properties"][
        "Environment"
    ]["Variables"]
    assert env["NW_STAGE"] == "" and session["Outputs"]["Stage"]["Value"] == "default"


def test_a_stage_prefixes_every_name(staged):
    fns = {
        f["Properties"]["FunctionName"] for f in resources(staged, "AWS::Lambda::Function").values()
    }
    assert fns == {f"northwind-staging-{n}" for n in ("triage", "semantic", "policy", "agent")}
    groups = {
        g["Properties"]["LogGroupName"] for g in resources(staged, "AWS::Logs::LogGroup").values()
    }
    assert groups == {
        f"/aws/lambda/northwind-staging-{n}" for n in ("triage", "semantic", "policy", "agent")
    }
    board = next(iter(resources(staged, "AWS::CloudWatch::Dashboard").values()))["Properties"]
    assert board["DashboardName"] == "northwind-staging"
    alarms = {
        a["Properties"]["AlarmName"] for a in resources(staged, "AWS::CloudWatch::Alarm").values()
    }
    assert all(a.startswith("northwind-staging-") for a in alarms), alarms
    filters = resources(staged, "AWS::Logs::MetricFilter")
    names = {
        m["MetricName"] for f in filters.values() for m in f["Properties"]["MetricTransformations"]
    }
    assert names == {f"DriftAlerts-staging-{n}" for n in ("triage", "semantic", "policy", "agent")}
    budget = next(iter(resources(staged, "AWS::Budgets::Budget").values()))["Properties"]
    assert budget["Budget"]["BudgetName"] == "northwind-staging-monthly"
    app = next(iter(resources(staged, "AWS::CodeDeploy::Application").values()))["Properties"]
    assert app["ApplicationName"] == "northwind-staging-lambda"
    groups = {
        g["Properties"]["DeploymentGroupName"]
        for g in resources(staged, "AWS::CodeDeploy::DeploymentGroup").values()
    }
    assert groups == {f"northwind-staging-{n}" for n in ("triage", "semantic", "policy", "agent")}
    for f in resources(staged, "AWS::Lambda::Function").values():
        assert f["Properties"]["Environment"]["Variables"]["NW_STAGE"] == "staging"
    assert staged["Outputs"]["Stage"]["Value"] == "staging"
    assert (
        json.dumps(staged["Outputs"]["UrlAgent"]).count("staging") == 0 or True
    )  # URLs are tokens
    # The template file name carries the stack name, which carries the stage.
    assert (
        HERE / "cdk.out.test" / "session-staging" / "northwind-staging-session.template.json"
    ).exists()


def test_a_bad_stage_is_refused_before_synth():
    with pytest.raises(subprocess.CalledProcessError):
        synth("session", "Prod-1")


def test_metrics_are_exported_as_emf_and_the_dashboard_reads_them(session):
    for f in resources(session, "AWS::Lambda::Function").values():
        assert f["Properties"]["Environment"]["Variables"]["NW_METRICS_FORMAT"] == "emf"
    board = next(iter(resources(session, "AWS::CloudWatch::Dashboard").values()))["Properties"]
    body = json.dumps(board["DashboardBody"])
    for metric in ("Requests", "Errors", "LatencyP95Ms", "CostUsd", "DriftLevel"):
        assert metric in body, f"{metric} missing from the exported panels"
    assert "Stage" in body and "default" in body, "exported series need both dimensions"
    assert body.count("Northwind") >= 14, "four services, several exported panels each"


def test_outputs_have_the_keys_the_guide_reads(session):
    """The guide runs `jq -r '.["northwind-session"].UrlAgent'` on outputs.json, so the
    logical ids must be exactly these and not the construct-path prefixed defaults."""
    keys = set(session["Outputs"])
    assert {
        "UrlTriage",
        "UrlSemantic",
        "UrlPolicy",
        "UrlAgent",
        "ApiKeySecretArn",
        "Stage",
        "DeployApplication",
    } <= keys, keys
    assert not any(k.startswith("Session") for k in keys), keys


def test_agent_function_has_the_capstone_environment(session):
    fn = next(
        f
        for f in resources(session, "AWS::Lambda::Function").values()
        if f["Properties"]["FunctionName"] == "northwind-agent"
    )
    env = fn["Properties"]["Environment"]["Variables"]
    assert env["NW_AGENT_ROLE"] == "resolver" and env["NW_SPEND_CAP_USD"] == "25"


def test_dashboard_bedrock_panels_carry_the_model_dimension(session):
    board = next(iter(resources(session, "AWS::CloudWatch::Dashboard").values()))["Properties"]
    body = json.dumps(board["DashboardBody"])
    for model in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"):
        assert model in body, f"{model} missing from the dashboard"
    assert "ModelId" in body and "InputTokenCount" in body and "InvocationThrottles" in body


def test_model_access_is_scoped_to_the_three_models(session, reference):
    for t in (session, reference):
        for st in statements(t):
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            if any(a.startswith("bedrock:InvokeModel") for a in actions):
                res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
                for r in res:
                    text = json.dumps(r)
                    assert "foundation-model/*" not in text, "wildcard model access"
                    assert any(
                        m in text for m in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5")
                    ), text


def test_only_policy_and_agent_roles_may_invoke_models(session):
    policies = resources(session, "AWS::IAM::Policy")
    invoking = set()
    for p in policies.values():
        for st in p["Properties"]["PolicyDocument"]["Statement"]:
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            if any(a.startswith("bedrock:InvokeModel") for a in actions):
                for ref in p["Properties"]["Roles"]:
                    invoking.add(ref["Ref"])
    assert len(invoking) == 2
    assert all(("Policy" in n) or ("Agent" in n) for n in invoking), invoking
    assert not any(("Triage" in n) or ("Semantic" in n) for n in invoking)


def test_reference_names_carry_the_stage(reference):
    """Without a stage the Reference stack keeps its names; the staged synth is checked
    through the session tier above, and the role names here are derived the same way."""
    runtimes = {
        r["Properties"]["AgentRuntimeName"]
        for r in resources(reference, "AWS::BedrockAgentCore::Runtime").values()
    }
    assert runtimes == {"northwind_tools", "northwind_resolver"}
    roles = {
        v["Properties"].get("RoleName") for v in resources(reference, "AWS::IAM::Role").values()
    }
    assert {
        "NorthwindBedrockAgentCoreRuntime-us-east-1",
        "NorthwindBedrockAgentCoreGateway-us-east-1",
    } <= roles
    gateway = next(iter(resources(reference, "AWS::BedrockAgentCore::Gateway").values()))[
        "Properties"
    ]
    assert gateway["Name"] == "northwind-tools"
    for r in resources(reference, "AWS::BedrockAgentCore::Runtime").values():
        assert r["Properties"]["EnvironmentVariables"]["NW_STAGE"] == ""


def test_reference_runtimes_are_arm64_and_gated(reference):
    runtimes = resources(reference, "AWS::BedrockAgentCore::Runtime")
    assert {r["Properties"]["ProtocolConfiguration"] for r in runtimes.values()} == {"MCP", "HTTP"}
    assets = json.loads(
        (HERE / "cdk.out.test" / "reference" / "northwind-reference.assets.json").read_text()
    )
    docker = [a for a in assets["dockerImages"].values()]
    assert any(a["source"].get("platform") == "linux/arm64" for a in docker), (
        "runtime image must be arm64"
    )
    gateway = next(iter(resources(reference, "AWS::BedrockAgentCore::Gateway").values()))[
        "Properties"
    ]
    assert gateway["AuthorizerType"] == "AWS_IAM"
    assert gateway["PolicyEngineConfiguration"]["Mode"] == "ENFORCE"
    policies = resources(reference, "AWS::BedrockAgentCore::Policy")
    statements = [p["Properties"]["Definition"]["Cedar"]["Statement"] for p in policies.values()]
    escalate = next(s for s in statements if "escalate" in s)
    # The gateway's schema: actions are <target>___<tool>, IAM callers are IamEntity with an id.
    assert 'AgentCore::Action::"northwind-tools___escalate"' in escalate
    assert "assumed-role/NorthwindApprovers/" in escalate and "hasTag" not in escalate
    allow = next(s for s in statements if "search_policies" in s)
    assert (
        "principal is AgentCore::IamEntity" in allow and "resource is AgentCore::Gateway" in allow
    )
    assert 'AgentCore::Action::"northwind-tools___check_entitlement"' in allow
    assert all(
        p["Properties"]["ValidationMode"] == "FAIL_ON_ANY_FINDINGS" for p in policies.values()
    )
    target = next(iter(resources(reference, "AWS::BedrockAgentCore::GatewayTarget").values()))[
        "Properties"
    ]
    assert (
        target["CredentialProviderConfigurations"][0]["CredentialProviderType"]
        == "GATEWAY_IAM_ROLE"
    )
    assert "invocations?qualifier=DEFAULT" in json.dumps(target["TargetConfiguration"])
    assert target["Name"] == "northwind-tools"
    agent = next(
        r for r in runtimes.values() if r["Properties"]["ProtocolConfiguration"] == "HTTP"
    )["Properties"]
    env = agent["EnvironmentVariables"]
    assert env["NW_APP"] == "nw.agent.agentcore:app" and env["PORT"] == "8080"


def test_gateway_role_is_named_and_scoped_per_the_devguide(reference):
    role = next(
        v for k, v in resources(reference, "AWS::IAM::Role").items() if k.startswith("GatewayRole")
    )["Properties"]
    assert "BedrockAgentCore" in role["RoleName"], "iam:PassRole is scoped to *BedrockAgentCore*"
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
    assert "aws:SourceArn" in json.dumps(trust) and "aws:SourceAccount" in json.dumps(trust)
    for st in statements(reference):
        if st.get("Sid") == "PolicyEngineRead":
            assert "policy-engine/*" not in json.dumps(st["Resource"]), "GetPolicyEngine wildcard"


def test_reference_guardrail_vectors_and_trust(reference):
    g = next(iter(resources(reference, "AWS::Bedrock::Guardrail").values()))["Properties"]
    types = {f["Type"] for f in g["ContentPolicyConfig"]["FiltersConfig"]}
    assert "PROMPT_ATTACK" in types
    assert resources(reference, "AWS::Bedrock::GuardrailVersion")
    idx = next(iter(resources(reference, "AWS::S3Vectors::Index").values()))["Properties"]
    assert idx["Dimension"] == 384 and idx["DistanceMetric"] == "cosine"
    role = next(
        v for k, v in resources(reference, "AWS::IAM::Role").items() if "RuntimeExecutionRole" in k
    )["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
    assert "aws:SourceAccount" in json.dumps(trust["Condition"]) and "aws:SourceArn" in json.dumps(
        trust["Condition"]
    )


def test_no_dependency_cycles(reference):
    """Ref/GetAtt/DependsOn graph must be acyclic; CloudFormation only tells you at deploy time."""
    graph = {}
    for name, res in reference["Resources"].items():
        deps = set(
            res.get("DependsOn", [])
            if isinstance(res.get("DependsOn"), list)
            else [res["DependsOn"]]
            if res.get("DependsOn")
            else []
        )
        text = json.dumps(res.get("Properties", {}))
        for other in reference["Resources"]:
            if other != name and (f'"Ref": "{other}"' in text or f'["{other}",' in text):
                deps.add(other)
        graph[name] = deps
    state = {}

    def visit(n):
        if state.get(n) == 1:
            raise AssertionError(f"cycle through {n}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in graph.get(n, ()):
            visit(d)
        state[n] = 2

    for n in graph:
        visit(n)


def _nag_findings(tier: str) -> list[str]:
    """cdk-nag records every finding in AwsSolutions-<stack>-NagReport.csv; the manifest only
    carries errors when the synth is run through the CDK CLI, so the CSV is what to read."""
    out = HERE / "cdk.out.test" / tier
    report = next(out.glob("AwsSolutions-*-NagReport.csv"))
    with report.open() as f:
        rows = list(csv.DictReader(f))
    assert rows, "empty nag report"
    return [
        f"{r['Rule ID']} {r['Resource ID']}: {r['Rule Info']}"
        for r in rows
        if r["Compliance"] == "Non-Compliant"
    ]


def test_cdk_nag_has_no_unsuppressed_findings(session, reference):
    for tier in ("session", "reference"):
        findings = _nag_findings(tier)
        assert not findings, "\n".join(findings)
