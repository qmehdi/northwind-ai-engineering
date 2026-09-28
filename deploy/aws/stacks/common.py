"""Shared pieces: images, model ARNs, budget, dashboard, alarms."""

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

REPO_ROOT = (
    Path(__file__).resolve().parents[3]
)  # the participant repo root (Dockerfile lives there)

# Model IDs the course uses. Bedrock ARNs for foundation models are region-less
# (`arn:aws:bedrock:*::foundation-model/<id>`); the instance role is scoped to
# exactly these three, never to `foundation-model/*`.
MODEL_IDS = {
    "workhorse": "anthropic.claude-sonnet-5",
    "judge": "anthropic.claude-opus-5",
    "economy": "anthropic.claude-haiku-4-5",
}

# A stage lets one account hold dev, staging and prod side by side: every name that must be
# unique in the account gets the stage after `northwind`. Empty means the names the guide
# uses. Seven characters at most, so the longest derived name (the GCP service account
# `northwind-<stage>-agent-engine`, 30 characters) fits on both tracks with the same word.
STAGE_RE = re.compile(r"^[a-z0-9]{0,7}$")


def check_stage(stage: str) -> str:
    if not STAGE_RE.match(stage):
        raise ValueError(f"stage {stage!r} must match {STAGE_RE.pattern}: lowercase, at most 7")
    return stage


def prefix(stage: str) -> str:
    return f"northwind-{stage}" if stage else "northwind"


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
    # Cross-region inference profiles live in the account; allow the same three by name.
    arns += [
        f"arn:aws:bedrock:{region}:{account}:inference-profile/*{m}" for m in MODEL_IDS.values()
    ]
    return arns


def bedrock_invoke_policy(scope: Construct) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid="InvokeCourseModels",
        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
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
        exclude=[
            ".venv",
            ".git",
            "tests",
            "notebooks",
            "docs",
            "deploy",
            "data/tickets.jsonl",
            "data/policies",
            "data/golden",
        ],
    )


def alerts_topic(scope: Construct, email: str | None, *, stage: str = "") -> sns.Topic:
    topic = sns.Topic(scope, "Alerts", display_name=f"{prefix(stage)}-alerts")
    if email:
        topic.add_subscription(subs.EmailSubscription(email))
    return topic


def monthly_budget(
    scope: Construct, *, limit_usd: float, email: str | None, stage: str = ""
) -> budgets.CfnBudget:
    """A cost budget with 50, 80 and 100 percent actual-spend alerts. The account-level
    alarm the PRD asks for; participants create it in their own account in Session 6."""
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
