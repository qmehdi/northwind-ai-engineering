"""Review before deploy: synthesise both tiers and check what a failed deploy would
otherwise teach us one rollback at a time."""

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


def synth(tier: str) -> dict:
    out = HERE / "cdk.out.test" / tier
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(
        [sys.executable, str(HERE / "app.py")],
        cwd=HERE,
        check=True,
        env={
            **os.environ,
            "CDK_CONTEXT_JSON": json.dumps(
                {"tier": tier, "alertEmail": "ops@example.com", "budgetUsd": 120}
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
    assert len(resources(session, "AWS::CloudWatch::Alarm")) == 8


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
    assert any(
        "escalate" in p["Properties"]["Definition"]["Cedar"]["Statement"] for p in policies.values()
    )
    target = next(iter(resources(reference, "AWS::BedrockAgentCore::GatewayTarget").values()))[
        "Properties"
    ]
    assert (
        target["CredentialProviderConfigurations"][0]["CredentialProviderType"]
        == "GATEWAY_IAM_ROLE"
    )
    assert "invocations?qualifier=DEFAULT" in json.dumps(target["TargetConfiguration"])


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


def _nag_errors(tier: str) -> list[str]:
    manifest = json.loads((HERE / "cdk.out.test" / tier / "manifest.json").read_text())
    out = []
    for art in manifest.get("artifacts", {}).values():
        for path, entries in art.get("metadata", {}).items():
            for e in entries:
                if e.get("type") == "aws:cdk:error":
                    out.append(f"{path}: {e.get('data')}")
    return out


def test_cdk_nag_has_no_unsuppressed_findings(session, reference):
    for tier in ("session", "reference"):
        errors = _nag_errors(tier)
        assert not errors, "\n".join(errors)
