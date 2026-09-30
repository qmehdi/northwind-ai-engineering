"""Shared pieces: names, models, images, budget, alerts.

The platform is one environment in one account (ADR 0008). `-c env=<word>` puts the word after
`northwind` in every name so a second environment (staging, prod) is a second deploy of the same
code, not a second codebase. Tenants (ADR 0009) are the learners: every resource a tenant owns is
named `northwind[-<env>]-<tenant>-<kind>`, which is exactly `Tenant.prefix` in `nw/platform/base.py`
when `NW_ENVIRONMENT` is `northwind[-<env>]`.
"""

from __future__ import annotations

import ast
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

# Model IDs per role (ADR 0010): an open-weight Workhorse and Economy, Claude as the Judge. One
# source: `nw/config.py` `DEFAULT_MODELS[Track.AWS]`, read with `ast` because this virtualenv
# does not have the course package. An id with a geo prefix (`us.`, `eu.`, `global.`) is a system
# cross-region inference profile; the application profiles copy from it. A model whose card has
# no in-region on-demand row in the stack's region is only served through a profile, so the
# stack uses the region's geo profile for it even when the config names the bare model id
# (Claude Opus 5 model card, fetched 2026-09-30: us-east-1 in-region "not supported", geo
# `us.anthropic.claude-opus-5`). Every role that may invoke a model is scoped to exactly these
# models and their profiles, never to `foundation-model/*`.
CONFIG = REPO_ROOT / "nw" / "config.py"
GEO_PREFIXES = ("us-gov.", "us.", "eu.", "apac.", "au.", "in.", "jp.", "global.")
PROFILE_ONLY = {"anthropic.claude-opus-5"}


def config_models(path: Path = CONFIG, table: str = "DEFAULT_MODELS") -> dict[str, str]:
    """`{"workhorse": ..., "judge": ..., "economy": ...}` from `<table>[Track.AWS]`
    (`DEFAULT_MODELS`, or `EU_MODELS` for the EU residency route; a None role is left out)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == table:
            assert isinstance(node.value, ast.Dict)
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                if isinstance(key, ast.Attribute) and key.attr == "AWS":
                    assert isinstance(value, ast.Dict)
                    return {
                        k.attr.lower(): v.value  # type: ignore[union-attr]
                        for k, v in zip(value.keys, value.values, strict=True)
                        if isinstance(v, ast.Constant) and v.value
                    }
    raise ValueError(f"{table}[Track.AWS] not found in {path}")


MODEL_IDS = config_models()
# EU residency (06 H5, `nw/config.py` `EU_MODELS`): the ids an EU account's calls use, invoked
# in `EU_REGION`. The gateway serves them as `eu/<tenant>/<role>` and `eu/<role>` for live.
EU_MODEL_IDS = config_models(table="EU_MODELS")
EU_REGION = "eu-central-1"
GATEWAY_EU_PREFIX = "eu/"


def base_model(model_id: str) -> str:
    """`us.amazon.nova-micro-v1:0` -> `amazon.nova-micro-v1:0`."""
    for geo in GEO_PREFIXES:
        if model_id.startswith(geo):
            return model_id[len(geo) :]
    return model_id


def geo(region: str) -> str:
    """The geo profile prefix that keeps a request in the region's geography."""
    if region.startswith(("us-", "ca-")):
        return "us"
    if region.startswith("eu-"):
        return "eu"
    return "global"


def system_profile(model_id: str, region: str) -> str | None:
    """The system inference profile a role goes through, or None for in-region on-demand."""
    if base_model(model_id) != model_id:
        return model_id
    if model_id in PROFILE_ONLY:
        return f"{geo(region)}.{model_id}"
    return None


def invoke_id(role: str, region: str) -> str:
    """What a caller passes as `modelId` for a role: the system profile or the model."""
    model_id = MODEL_IDS[role]
    return system_profile(model_id, region) or model_id


def copy_from_arn(role: str, region: str, account: str) -> str:
    """The `CopyFrom` of a role's application inference profile."""
    model_id = MODEL_IDS[role]
    profile = system_profile(model_id, region)
    if profile:
        return f"arn:aws:bedrock:{region}:{account}:inference-profile/{profile}"
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


# Compute a tenant (or anything a tenant starts) may use: the sizes `nw/pipelines/sagemaker`
# and `nw/platform/aws.py` ask for, Studio's default, and `system` (the Studio server app). Any
# other instance type, and any accelerator, is denied by `instance_type_guard` on every tenant
# role and on the deployer; serverless endpoint configs name no instance type and pass.
INSTANCE_TYPES = ("ml.t3.medium", "ml.m5.large", "ml.m5.xlarge", "ml.m5.2xlarge", "system")
GUARDED_ACTIONS = (
    "sagemaker:CreateTrainingJob",
    "sagemaker:CreateProcessingJob",
    "sagemaker:CreateTransformJob",
    "sagemaker:CreateHyperParameterTuningJob",
    "sagemaker:CreateEndpointConfig",
    "sagemaker:CreateNotebookInstance",
    "sagemaker:CreateApp",
    "sagemaker:CreateMonitoringSchedule",
    "sagemaker:CreateDataQualityJobDefinition",
)

# The prebuilt framework containers the model packages serve with (`nw/serving/sagemaker`,
# SageMaker Python SDK image_uri_config, fetched 2026-09-29), per region. Anything else must be
# an image in this account's own `northwind[-<env>]-*` repositories.
DLC_REPOSITORIES = {
    "us-east-1": (
        ("683313688378", "sagemaker-scikit-learn"),
        ("763104351884", "pytorch-inference"),
    ),
}


def instance_type_guard() -> iam.PolicyStatement:
    """Deny compute outside `INSTANCE_TYPES`; `sagemaker:InstanceTypes` is multivalued."""
    return iam.PolicyStatement(
        sid="OnlyCourseInstanceTypes",
        effect=iam.Effect.DENY,
        actions=list(GUARDED_ACTIONS),
        resources=["*"],
        conditions={"ForAnyValue:StringNotLike": {"sagemaker:InstanceTypes": list(INSTANCE_TYPES)}},
    )


def accelerator_guard() -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid="NoAccelerators",
        effect=iam.Effect.DENY,
        actions=["sagemaker:CreateEndpointConfig", "sagemaker:CreateNotebookInstance"],
        resources=["*"],
        conditions={"ForAnyValue:StringLike": {"sagemaker:AcceleratorTypes": ["*"]}},
    )


def ecr_pull_statement(scope: Construct, prefix: str) -> iam.PolicyStatement:
    """ECR pull pinned to this account's platform repositories and the listed framework images."""
    stack = Stack.of(scope)
    resources = [
        f"arn:aws:ecr:{stack.region}:{stack.account}:repository/{prefix}-*",
        # the CDK bootstrap repository holds the images built at deploy
        f"arn:aws:ecr:{stack.region}:{stack.account}:repository/cdk-*-container-assets-*",
    ]
    resources += [
        f"arn:aws:ecr:{stack.region}:{account}:repository/{repo}"
        for account, repo in DLC_REPOSITORIES.get(stack.region, ())
    ]
    return iam.PolicyStatement(
        sid="EcrPull",
        actions=[
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchCheckLayerAvailability",
        ],
        resources=resources,
    )


def allowed_image_prefixes(scope: Construct, prefix: str) -> list[str]:
    """Image URI prefixes the approval deployer accepts in a model package."""
    stack = Stack.of(scope)
    out = [f"{stack.account}.dkr.ecr.{stack.region}.amazonaws.com/{prefix}-"]
    out += [
        f"{account}.dkr.ecr.{stack.region}.amazonaws.com/{repo}{sep}"
        for account, repo in DLC_REPOSITORIES.get(stack.region, ())
        for sep in (":", "@")
    ]
    return out


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


# Words a tenant may not be called. The first group are the kinds the stack itself puts after
# `northwind[-<env>]-` (a tenant named `gateway` would own `northwind-gateway-...` names the
# platform already uses); the second are environment words, because `northwind-staging-...` is
# the staging environment's namespace and a tenant called `staging` in the default environment
# would collide with it (and its approvals would look like staging's).
RESERVED_NAMES = frozenset(
    {
        LIVE,
        "platform",
        "mlflow",
        "serving",
        "deployer",
        "delivery",
        "build",
        "deploy",
        "pipeline",
        "pipelines",
        "gateway",
        "data",
        "artifacts",
        "logs",
        "trail",
        "vectors",
        "knowledge",
        "web",
        "tools",
        "agents",
        "agent",
        "policy",
        "alerts",
        "monthly",
        "lambda",
        "approvers",
        "images",
        "monitor",
        "domain",
        "service",
        "learner",
        "admin",
        "root",
    }
    | {"dev", "test", "qa", "uat", "stage", "staging", "preprod", "prod", "sandbox", "demo"}
)


def tenant_names(raw: str | None, mode: str, env_name: str = "") -> list[str]:
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
        if name in RESERVED_NAMES or (env_name and name == env_name):
            raise ValueError(f"tenant {name!r} is reserved (a platform or environment word)")
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


def model_arns(scope: Construct, roles: tuple[str, ...] | None = None) -> list[str]:
    """Foundation models in every region (a geo profile routes across regions), the system
    profiles in this region, and the account's application profiles."""
    region = Stack.of(scope).region
    account = Stack.of(scope).account
    roles = roles or tuple(MODEL_IDS)
    arns = [f"arn:aws:bedrock:*::foundation-model/{base_model(MODEL_IDS[r])}" for r in roles]
    arns += [
        f"arn:aws:bedrock:{region}:{account}:inference-profile/{p}"
        for r in roles
        if (p := system_profile(MODEL_IDS[r], region))
    ]
    arns.append(f"arn:aws:bedrock:{region}:{account}:application-inference-profile/*")
    # The EU route: the geo profiles in the EU region (the foundation models above are already
    # region-wildcarded, which covers the EU profile's destination regions).
    arns += [
        f"arn:aws:bedrock:{EU_REGION}:{account}:inference-profile/{EU_MODEL_IDS[r]}"
        for r in roles
        if r in EU_MODEL_IDS and base_model(EU_MODEL_IDS[r]) != EU_MODEL_IDS[r]
    ]
    arns += [
        f"arn:aws:bedrock:*::foundation-model/{base_model(EU_MODEL_IDS[r])}"
        for r in roles
        if r in EU_MODEL_IDS and base_model(EU_MODEL_IDS[r]) != base_model(MODEL_IDS[r])
    ]
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


def git_sha() -> str:
    """The commit the stack builds its images from (`GIT_SHA`, kept in the image as
    `NW_IMAGE_GIT_SHA` for lineage), or empty outside a git checkout."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


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
        build_args={"APP": app, "ARTIFACTS": artifacts, "HF_MODELS": hf, "GIT_SHA": git_sha()},
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


def image_parameter(prefix: str, name: str) -> str:
    """The SSM parameter that owns a service's promoted image URI (`asset` until promoted)."""
    return f"/{prefix}/images/{name}"


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
