"""SageMaker Pipelines definitions: the AWS track.

    python -m nw.pipelines.sagemaker.definition --pipeline triage --tenant alice \
        --role arn:aws:iam::123456789012:role/northwind-pipelines \
        --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/nw-pipelines:latest --bucket nw-bucket

The same steps as the Kubeflow definition, each a ProcessingStep running the course image
with `python -m nw.pipelines.steps.<name>`, then a ConditionStep on the gate's `passed_int`
that either registers the candidate into the tenant's model package group with
`PendingManualApproval` or fails the run with the gate's reasons. `definition()` builds the
pipeline JSON on a fake session, the way the SDK's own tests do, so it is unit tested and
never calls AWS; the AWS platform passes a real `PipelineSession` and calls `upsert`.

Verified against the SageMaker Python SDK v3 (`sagemaker` 3.23.0, `sagemaker-mlops` 1.23.0,
released 2026-09-24): `sagemaker.mlops.workflow.pipeline.Pipeline`, `steps.ProcessingStep`,
`condition_step.ConditionStep`, `model_step.ModelStep`, `fail_step.FailStep`;
`sagemaker.core.workflow` for parameters, conditions, `JsonGet`, `Join`, `PropertyFile`,
`ExecutionVariables` and `PipelineSession`; `sagemaker.core.processing.Processor`;
`sagemaker.serve.model_builder.ModelBuilder.register` for the register step
(docs: sagemaker.readthedocs.io/en/stable/ml_ops/index.html and the v3 pipeline example,
fetched 2026-09-29).
"""

from __future__ import annotations

from nw.pipelines.sagemaker.definitions import (
    SageMakerConfig,
    definition,
    fake_session,
    semantic_pipeline,
    triage_pipeline,
)

__all__ = ["SageMakerConfig", "definition", "fake_session", "semantic_pipeline", "triage_pipeline"]
