"""Shared pieces: names, models, images, budget, alerts.

The platform is one environment in one account (ADR 0008). `-c env=<word>` puts the word after
`northwind` in every name so a second environment (staging, prod) is a second deploy of the same
code, not a second codebase. Tenants (ADR 0009) are the learners: every resource a tenant owns is
named `northwind[-<env>]-<tenant>-<kind>`, which is exactly `Tenant.prefix` in `nw/platform/base.py`
when `NW_ENVIRONMENT` is `northwind[-<env>]`.
"""

from __future__ import annotations

import re
from pathlib import Path

from aws_cdk import Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[3]  # the repo root (Dockerfile lives there)

# Model IDs per role (ADR 0010): an open-weight Workhorse and Economy, Claude as the Judge. Bedrock
# foundation-model ARNs are region-less (`arn:aws:bedrock:*::foundation-model/<id>`); every role that
# may invoke a model is scoped to exactly these three plus the account's inference profiles for
# them, never to `foundation-model/*`. A model that a region only serves through a cross-region
# profile takes the `us.` profile id here instead.
MODEL_IDS = {
    "workhorse": "openai.gpt-oss-120b-1:0",
    "judge": "anthropic.claude-opus-5",
    "economy": "amazon.nova-micro-v1:0",
}
# The Knowledge Base embedding model and its dimension (S3 Vectors index dimension must match).
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIM = 1024

# The registry-backed projects: one Model Package Group and one endpoint name per tenant each.
PROJECTS = ("triage", "semantic")
# The live target is a pseudo-tenant: the promoted endpoint, knowledge base and agent runtime.
LIVE = "live"

# Names that must be unique in the account get the environment word after `northwind`. Seven
# characters at most, so the longest derived name fits every service's limit.
STAGE_RE = re.compile(r"^[a-z0-9]{0,7}$")
TENANT_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")  # the same rule as nw.platform.base.TENANT_RE


def check_stage(stage: str) -> str:
    if not STAGE_RE.match(stage):
        raise ValueError(f"env {stage!r} must match {STAGE_RE.pattern}: lowercase, at most 7")
    return stage


def prefix(stage: str) -> str:
    return f"northwind-{stage}" if stage else "northwind"


def tenant_names(raw: str | None, mode: str) -> list[str]:
    """`-c tenants=alice,bob` (cohort) or `-c mode=solo` (one tenant named `solo`)."""
    if raw:
        names = [t.strip() for t in raw.split(",") if t.strip()]
    elif mode == "solo":
        names = ["solo"]
    else:
        raise ValueError("cohort mode needs -c tenants=<name,name,...>; or pass -c mode=solo")
    for name in names:
        if not TENANT_RE.match(name):
            raise ValueError(f"tenant {name!r}: 2 to 16 lowercase letters or digits, letter first")
        if name == LIVE:
            raise ValueError("`live` is the promoted target, not a tenant name")
    if len(set(names)) != len(names):
        raise ValueError("duplicate tenant names")
    return names


SERVICES = {
    # name: (asgi app, artifacts baked in, hf models, cpu vcpu, memory gb)
    "triage": ("nw.triage.service:app", "triage", "0", 1, 2),
    "semantic": ("nw.semantic.service:app", "semantic index", "1", 1, 3),
    "policy": ("nw.policy.service:app", "policy", "1", 1, 3),
    "agent": ("nw.agent.service:app", "triage semantic index policy", "1", 2, 4),
}


def model_arns(scope: Construct) -> list[str]:
    region = Stack.of(scope).region
    account = Stack.of(scope).account
    arns = [f"arn:aws:bedrock:*::foundation-model/{m}" for m in MODEL_IDS.values()]
    # Application and cross-region inference profiles live in the account; the application
    # profiles this stack creates copy from these three models and carry the tenant tag.
    arns += [
        f"arn:aws:bedrock:{region}:{account}:inference-profile/*{m}" for m in MODEL_IDS.values()
    ]
    arns.append(f"arn:aws:bedrock:{region}:{account}:application-inference-profile/*")
    return arns


def bedrock_invoke_policy(scope: Construct) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid="InvokeCourseModels",
        actions=[
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:Converse",
            "bedrock:ConverseStream",
        ],
        resources=model_arns(scope),
    )


def image(
    scope: Construct, name: str, *, platform: ecr_assets.Platform
) -> ecr_assets.DockerImageAsset:
    app, artifacts, hf, _cpu, _mem = SERVICES[name]
    return ecr_assets.DockerImageAsset(
        scope,
        f"Image{name.title()}{platform.platform.split('/')[-1]}",
        directory=str(REPO_ROOT),
        file="Dockerfile",
        platform=platform,
        build_args={"APP": app, "ARTIFACTS": artifacts, "HF_MODELS": hf},
        exclude=IMAGE_EXCLUDE,
    )


# The build context, as .dockerignore has it: the golden sets stay out except the two
# production summaries every image carries (the Dockerfile copies them).
IMAGE_EXCLUDE = [
    ".venv",
    ".git",
    "tests",
    "notebooks",
    "docs",
    "deploy",
    "data/tickets.jsonl",
    "data/policies",
    "data/golden/*",
    "!data/golden/triage_production.json",
    "!data/golden/semantic_production.json",
]


def alerts_topic(scope: Construct, email: str | None, *, stage: str = "") -> sns.Topic:
    topic = sns.Topic(scope, "Alerts", display_name=f"{prefix(stage)}-alerts")
    if email:
        topic.add_subscription(subs.EmailSubscription(email))
    return topic


def monthly_budget(
    scope: Construct, *, limit_usd: float, email: str | None, stage: str = ""
) -> budgets.CfnBudget:
    """A cost budget with 50, 80 and 100 percent actual-spend alerts: the account-level alarm."""
    subscribers = (
        [budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)]
        if email
        else []
    )
    notifications = (
        [
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="ACTUAL",
                    comparison_operator="GREATER_THAN",
                    threshold=pct,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=subscribers,
            )
            for pct in (50, 80, 100)
        ]
        if email
        else []
    )
    return budgets.CfnBudget(
        scope,
        "MonthlyBudget",
        budget=budgets.CfnBudget.BudgetDataProperty(
            budget_name=f"{prefix(stage)}-monthly",
            budget_type="COST",
            time_unit="MONTHLY",
            budget_limit=budgets.CfnBudget.SpendProperty(amount=limit_usd, unit="USD"),
        ),
        notifications_with_subscribers=notifications or None,
    )
