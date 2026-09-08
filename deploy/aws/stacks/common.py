"""Shared pieces: images, model ARNs, budget, dashboard, alarms."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
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


def alerts_topic(scope: Construct, email: str | None) -> sns.Topic:
    topic = sns.Topic(scope, "Alerts", display_name="northwind-alerts")
    if email:
        topic.add_subscription(subs.EmailSubscription(email))
    return topic


def monthly_budget(scope: Construct, *, limit_usd: float, email: str | None) -> budgets.CfnBudget:
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
            budget_name="northwind-monthly",
            budget_type="COST",
            time_unit="MONTHLY",
            budget_limit=budgets.CfnBudget.SpendProperty(amount=limit_usd, unit="USD"),
        ),
        notifications_with_subscribers=notifications or None,
    )


def service_alarms(scope: Construct, service_name: str, topic: sns.Topic) -> list[cw.Alarm]:
    """App Runner emits 5xxStatusResponses, RequestLatency and ActiveInstances per service."""
    dims = {"ServiceName": service_name}
    errors = cw.Alarm(
        scope,
        f"Alarm5xx{service_name}",
        metric=cw.Metric(
            namespace="AWS/AppRunner",
            metric_name="5xxStatusResponses",
            dimensions_map=dims,
            statistic="Sum",
            period=Duration.minutes(5),
        ),
        threshold=5,
        evaluation_periods=2,
        alarm_description=f"{service_name}: more than 5 server errors in 5 minutes, twice",
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    latency = cw.Alarm(
        scope,
        f"AlarmLatency{service_name}",
        metric=cw.Metric(
            namespace="AWS/AppRunner",
            metric_name="RequestLatency",
            dimensions_map=dims,
            statistic="p95",
            period=Duration.minutes(5),
        ),
        threshold=8000,
        evaluation_periods=3,
        alarm_description=f"{service_name}: p95 latency above 8 seconds for 15 minutes",
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    for a in (errors, latency):
        a.add_alarm_action(
            __import__("aws_cdk.aws_cloudwatch_actions", fromlist=["SnsAction"]).SnsAction(topic)
        )
    return [errors, latency]


def dashboard(scope: Construct, service_names: list[str]) -> cw.Dashboard:
    board = cw.Dashboard(scope, "Dashboard", dashboard_name="northwind")
    for name in service_names:
        dims = {"ServiceName": name}
        board.add_widgets(
            cw.GraphWidget(
                title=f"{name} requests and errors",
                left=[
                    cw.Metric(
                        namespace="AWS/AppRunner",
                        metric_name="Requests",
                        dimensions_map=dims,
                        statistic="Sum",
                    ),
                    cw.Metric(
                        namespace="AWS/AppRunner",
                        metric_name="5xxStatusResponses",
                        dimensions_map=dims,
                        statistic="Sum",
                    ),
                ],
            ),
            cw.GraphWidget(
                title=f"{name} latency p50 p95",
                left=[
                    cw.Metric(
                        namespace="AWS/AppRunner",
                        metric_name="RequestLatency",
                        dimensions_map=dims,
                        statistic="p50",
                    ),
                    cw.Metric(
                        namespace="AWS/AppRunner",
                        metric_name="RequestLatency",
                        dimensions_map=dims,
                        statistic="p95",
                    ),
                ],
            ),
        )
    board.add_widgets(
        cw.GraphWidget(
            title="Bedrock tokens",
            left=[
                cw.Metric(namespace="AWS/Bedrock", metric_name="InputTokenCount", statistic="Sum"),
                cw.Metric(namespace="AWS/Bedrock", metric_name="OutputTokenCount", statistic="Sum"),
            ],
        ),
        cw.GraphWidget(
            title="Bedrock invocations and throttles",
            left=[
                cw.Metric(namespace="AWS/Bedrock", metric_name="Invocations", statistic="Sum"),
                cw.Metric(
                    namespace="AWS/Bedrock", metric_name="InvocationThrottles", statistic="Sum"
                ),
            ],
        ),
    )
    return board
