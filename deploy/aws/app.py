#!/usr/bin/env python3
"""CDK app for the AWS track: one platform stack per environment (ADR 0008, 0009, 0012).

    cdk synth -c mode=solo                                   # one tenant named solo
    cdk synth -c tenants=alice,bob -c alertEmail=you@example.com -c budgetUsd=300
    cdk synth -c tenants=alice -c env=staging                # northwind-staging-* names
    cdk synth -c tenants=alice -c connectionArn=arn:aws:codeconnections:...  # with the pipeline
    cdk synth -c mode=solo -c agentEgress=public             # agents without the egress VPC

cdk-nag runs on every synth with the AwsSolutions pack; a new finding fails the synth until it
is fixed or suppressed with a reason in stacks/nag.py.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.common import check_stage, prefix, tenant_names
from stacks.nag import suppress_known
from stacks.platform import PlatformStack

app = cdk.App()
ctx = app.node.try_get_context
env_name = check_stage(ctx("env") or "")
mode = ctx("mode") or ("cohort" if ctx("tenants") else "solo")
tenants = tenant_names(ctx("tenants"), mode, env_name)
stack = PlatformStack(
    app,
    f"{prefix(env_name)}-platform" if env_name else "northwind-platform",
    env_name=env_name,
    tenants=tenants,
    mode=mode,
    budget_usd=float(ctx("budgetUsd") or 200),
    alert_email=ctx("alertEmail") or None,
    connection_arn=ctx("connectionArn") or None,
    github_owner=ctx("githubOwner") or "techwithshadab",
    github_repo=ctx("githubRepo") or "northwind-ai-engineering",
    github_branch=ctx("githubBranch") or "main",
    lake_formation=str(ctx("lakeFormation") or "false").lower() == "true",
    agent_egress=str(ctx("agentEgress") or "vpc").lower(),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)
if ctx("nag") != "false":
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    suppress_known(stack)
app.synth()
