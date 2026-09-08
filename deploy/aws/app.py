#!/usr/bin/env python3
"""CDK app for the AWS track.

    cdk synth -c tier=session
    cdk synth -c tier=reference -c alertEmail=you@example.com -c budgetUsd=150

cdk-nag runs on every synth with the AwsSolutions pack; a new finding fails the
synth until it is fixed or suppressed with a reason in stacks/nag.py.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.nag import suppress_known
from stacks.reference import ReferenceStack
from stacks.session_path import SessionStack

app = cdk.App()
tier = app.node.try_get_context("tier") or "session"
budget = float(app.node.try_get_context("budgetUsd") or 100)
email = app.node.try_get_context("alertEmail")
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

if tier == "reference":
    stack = ReferenceStack(
        app, "northwind-reference", budget_usd=budget, alert_email=email, env=env
    )
else:
    stack = SessionStack(app, "northwind-session", budget_usd=budget, alert_email=email, env=env)

if app.node.try_get_context("nag") != "false":
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    suppress_known(stack)
app.synth()
