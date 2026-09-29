"""Pipelines: the EventBridge schedules that start each tenant's retraining pipelines weekly,
and the production summaries their gates compare against.

The pipeline definitions themselves are code (`nw/pipelines`, SageMaker SDK) and are upserted
by the tenant with `nw.platform.PipelineRunner`, so the stack holds only what the definition
cannot: the role a job runs with (the tenant's execution role, `areas/tracking.py`), the
schedules, and the committed production summaries (`data/golden/*_production.json`), copied to
`s3://<artifacts>/baselines/` where the gate step reads them (`ProductionSummary`). Every
schedule is DISABLED at deploy: a learner enables one in the pipelines step with
`aws events enable-rule`, and the weekly candidate it produces never promotes by itself,
because promotion is the approval on the model package.

A rule passes only parameters the definition declares (`nw/pipelines/params.py`; the synth
review checks): `Trigger=schedule`, so a scheduled run is told apart from a hand submission on
the registered version, and the S3 locations of the data, the output root and the production
summary, so a scheduled run is right even when the definition was upserted without them.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from stacks.areas.tracking import sagemaker_arn
from stacks.common import REPO_ROOT

PIPELINES = ("triage", "semantic")
BASELINES = "baselines"
GOLDEN = REPO_ROOT / "data" / "golden"


def production_summary_key(pipeline: str) -> str:
    """`baselines/triage_production.json`: where the gate's production summary lives."""
    return f"{BASELINES}/{pipeline}_production.json"


class Pipelines(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tenants: list[str],
        data: s3.IBucket,
        artifacts: s3.IBucket,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.baselines = s3deploy.BucketDeployment(
            self,
            "Baselines",
            sources=[
                s3deploy.Source.data(
                    production_summary_key(p),
                    (GOLDEN / f"{p}_production.json").read_text(encoding="utf-8"),
                )
                for p in PIPELINES
            ],
            destination_bucket=artifacts,
            prune=False,
        )
        role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{prefix}-pipeline-scheduler",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            description="EventBridge starts the retraining pipelines on a schedule",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="StartRetraining",
                actions=["sagemaker:StartPipelineExecution"],
                resources=[sagemaker_arn(self, "pipeline", f"{prefix}-*-{p}") for p in PIPELINES],
            )
        )
        self.rules: dict[tuple[str, str], events.CfnRule] = {}
        for tenant in tenants:
            for pipeline in PIPELINES:
                parameters = {
                    "Trigger": "schedule",
                    "DataUri": f"s3://{data.bucket_name}/data/tickets/",
                    "OutputRoot": f"s3://{artifacts.bucket_name}/tenants/{tenant}/pipelines",
                    "ProductionSummary": f"s3://{artifacts.bucket_name}/{production_summary_key(pipeline)}",
                }
                rule = events.CfnRule(
                    self,
                    f"Retrain{pipeline.title()}{tenant.title()}",
                    name=f"{prefix}-{tenant}-retrain-{pipeline}",
                    description=f"Weekly {pipeline} retraining candidate for {tenant}; disabled until enabled on purpose",
                    schedule_expression="cron(0 3 ? * MON *)",
                    state="DISABLED",
                    targets=[
                        events.CfnRule.TargetProperty(
                            id="pipeline",
                            arn=sagemaker_arn(self, "pipeline", f"{prefix}-{tenant}-{pipeline}"),
                            role_arn=role.role_arn,
                            sage_maker_pipeline_parameters=events.CfnRule.SageMakerPipelineParametersProperty(
                                pipeline_parameter_list=[
                                    events.CfnRule.SageMakerPipelineParameterProperty(
                                        name=name, value=value
                                    )
                                    for name, value in parameters.items()
                                ]
                            ),
                        )
                    ],
                )
                # The first scheduled run must find the summaries in place.
                rule.node.add_dependency(self.baselines)
                self.rules[(tenant, pipeline)] = rule
        self.role = role
        self.account = stack.account
        CfnOutput(
            self, "OutBaselinesUri", value=f"s3://{artifacts.bucket_name}/{BASELINES}/"
        ).override_logical_id("BaselinesUri")
