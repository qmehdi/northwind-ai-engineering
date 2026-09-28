#!/usr/bin/env python3
"""CDK app for the AWS track.

    cdk synth -c tier=session
    cdk synth -c tier=reference -c alertEmail=you@example.com -c budgetUsd=150
    cdk synth -c tier=session -c stage=staging     # northwind-staging-* names, stack northwind-staging-session

cdk-nag runs on every synth with the AwsSolutions pack; a new finding fails the
synth until it is fixed or suppressed with a reason in stacks/nag.py.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.common import check_stage, prefix
from stacks.nag import suppress_known
from stacks.reference import ReferenceStack
from stacks.session_path import SessionStack

app = cdk.App()
tier = app.node.try_get_context("tier") or "session"
budget = float(app.node.try_get_context("budgetUsd") or 100)
email = app.node.try_get_context("alertEmail")
stage = check_stage(app.node.try_get_context("stage") or "")
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

if tier == "reference":
    stack = ReferenceStack(
        app,
        f"{prefix(stage)}-reference",
        budget_usd=budget,
        alert_email=email,
        stage=stage,
        env=env,
    )
else:
    stack = SessionStack(
        app, f"{prefix(stage)}-session", budget_usd=budget, alert_email=email, stage=stage, env=env
    )

if app.node.try_get_context("nag") != "false":
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    suppress_known(stack)
app.synth()
