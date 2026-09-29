"""The SageMaker pipeline graphs. SDK imports are inside the functions so the module loads
without the SDK and the tests can skip cleanly when the `pipelines` extra is absent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from nw.pipelines.params import BY_PIPELINE, Param
from nw.platform.base import Tenant

ARTIFACTS = "/opt/ml/processing/artifacts"
DATA_DIR = "/opt/ml/processing/input/data"
PRODUCTION_DIR = "/opt/ml/processing/input/production"
CONTENT_TYPES = ["application/json"]


@dataclass(frozen=True)
class SageMakerConfig:
    """What a definition needs beyond its parameters. Nothing here is a secret."""

    tenant: Tenant
    role_arn: str
    image_uri: str  # the nw-pipelines image in ECR
    bucket: str
    region: str = "us-east-1"
    serving_image_uri: str | None = None  # the image the registered package serves with
    instance_type: str = "ml.m5.xlarge"
    training_instance_type: str = "ml.m5.2xlarge"
    inference_instance_type: str = "ml.m5.large"
    volume_size_gb: int = 30
    # Parameter defaults that replace the repo paths of `nw.pipelines.params` in the definition
    # (`data_uri`, `output_root`, `production_summary` as S3 URIs), so a run that passes
    # nothing, the weekly schedule's included, reads and writes the deployed locations.
    defaults: Mapping[str, Any] = field(default_factory=dict)

    @property
    def serving_image(self) -> str:
        return self.serving_image_uri or self.image_uri

    def pipeline_name(self, pipeline: str) -> str:
        """`northwind-alice-triage`: the name `SageMakerPipelines` upserts and starts, the
        EventBridge schedule targets and the execution role's policy covers."""
        return self.tenant.resource(pipeline)

    def group_name(self, pipeline: str) -> str:
        return self.tenant.resource(pipeline)


def fake_session(region: str = "us-east-1", bucket: str = "nw-bucket") -> Any:
    """A `PipelineSession` over mocked boto clients: enough to build a definition,
    unable to reach AWS. The SDK's own workflow tests build sessions the same way."""
    from unittest.mock import MagicMock

    from sagemaker.core.workflow.pipeline_context import PipelineSession

    boto_session = MagicMock(name="boto_session", region_name=region)
    return PipelineSession(
        boto_session=boto_session,
        sagemaker_client=MagicMock(name="sagemaker_client"),
        default_bucket=bucket,
    )


def _parameters(pipeline: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from sagemaker.core.workflow.parameters import (
        ParameterFloat,
        ParameterInteger,
        ParameterString,
    )

    overrides = overrides or {}
    out: dict[str, Any] = {}
    for p in BY_PIPELINE[pipeline]:
        if not p.sagemaker:
            continue
        name = p.sagemaker_name
        default = overrides.get(p.name, p.default)
        if p.kind is bool:
            out[p.name] = ParameterString(name, default_value="true" if default else "false")
        elif p.kind is float:
            out[p.name] = ParameterFloat(name, default_value=float(default))
        elif p.kind is int:
            out[p.name] = ParameterInteger(name, default_value=int(default))
        else:
            out[p.name] = ParameterString(name, default_value=str(default))
    return out


def _flags(params: dict[str, Any], names: tuple[Param, ...], *which: str) -> list[Any]:
    """`--min-p0-recall <ParameterFloat>` for each named parameter."""
    out: list[Any] = []
    for name in which:
        out += [f"--{name.replace('_', '-')}", params[name]]
    return out


def _artifacts_uri(params: dict[str, Any], pipeline: str) -> Any:
    from sagemaker.core.workflow.execution_variables import ExecutionVariables
    from sagemaker.core.workflow.functions import Join

    return Join(
        on="/",
        values=[
            params["output_root"],
            pipeline,
            ExecutionVariables.PIPELINE_EXECUTION_ID,
            "artifacts",
        ],
    )


def _processing_step(
    name: str,
    module: str,
    config: SageMakerConfig,
    session: Any,
    *,
    arguments: list[Any],
    inputs: list[tuple[str, Any, str]],
    artifacts_out: Any,
    instance_type: str | None = None,
    depends_on: list[Any] | None = None,
    property_files: list[Any] | None = None,
) -> Any:
    from sagemaker.core.processing import Processor
    from sagemaker.core.shapes import (
        ProcessingInput,
        ProcessingOutput,
        ProcessingS3Input,
        ProcessingS3Output,
    )
    from sagemaker.mlops.workflow.steps import ProcessingStep

    processor = Processor(
        role=config.role_arn,
        image_uri=config.image_uri,
        instance_count=1,
        instance_type=instance_type or config.instance_type,
        volume_size_in_gb=config.volume_size_gb,
        entrypoint=["python", "-m", module],
        base_job_name=config.tenant.resource(name.lower()),
        sagemaker_session=session,
        env={
            "NW_TENANT": config.tenant.name,
            "NW_ENVIRONMENT": config.tenant.environment,
            "NW_TRACK": "aws",
            "TOKENIZERS_PARALLELISM": "false",
        },
    )
    step_inputs = [
        ProcessingInput(
            input_name=input_name,
            s3_input=ProcessingS3Input(
                s3_uri=s3_uri,
                local_path=local_path,
                s3_data_type="S3Prefix",
                s3_input_mode="File",
            ),
        )
        for input_name, s3_uri, local_path in inputs
    ]
    outputs = [
        ProcessingOutput(
            output_name="artifacts",
            s3_output=ProcessingS3Output(
                s3_uri=artifacts_out, local_path=ARTIFACTS, s3_upload_mode="EndOfJob"
            ),
        )
    ]
    return ProcessingStep(
        name=name,
        step_args=processor.run(inputs=step_inputs, outputs=outputs, arguments=arguments),
        depends_on=depends_on,
        property_files=property_files,
    )


def _artifacts_of(step: Any) -> Any:
    return step.properties.ProcessingOutputConfig.Outputs["artifacts"].S3Output.S3Uri


def _register_and_gate(
    pipeline: str,
    config: SageMakerConfig,
    session: Any,
    *,
    trigger: Any,
    gate_step: Any,
    gate_file: Any,
    model_artifacts_uri: Any,
) -> Any:
    from sagemaker.core.workflow.conditions import ConditionGreaterThanOrEqualTo
    from sagemaker.core.workflow.functions import Join, JsonGet
    from sagemaker.mlops.workflow.condition_step import ConditionStep
    from sagemaker.mlops.workflow.fail_step import FailStep
    from sagemaker.mlops.workflow.model_step import ModelStep
    from sagemaker.serve.model_builder import ModelBuilder

    builder = ModelBuilder(
        s3_model_data_url=Join(on="/", values=[model_artifacts_uri, "model.tar.gz"]),
        image_uri=config.serving_image,
        sagemaker_session=session,
        role_arn=config.role_arn,
    )
    register = ModelStep(
        name="Register",
        step_args=builder.register(
            model_package_group_name=config.group_name(pipeline),
            content_types=CONTENT_TYPES,
            response_types=CONTENT_TYPES,
            inference_instances=[config.inference_instance_type],
            approval_status="PendingManualApproval",
            customer_metadata_properties={
                "pipeline": pipeline,
                "tenant": config.tenant.prefix,
                "environment": config.tenant.environment,
                "trigger": Join(on="", values=[trigger]),
                "gate": Join(
                    on="",
                    values=[
                        JsonGet(
                            step_name=gate_step.name, property_file=gate_file, json_path="reason"
                        )
                    ],
                ),
            },
        ),
    )
    failed = FailStep(
        name="GateFailed",
        error_message=Join(
            on=" ",
            values=[
                "gate failed:",
                JsonGet(step_name=gate_step.name, property_file=gate_file, json_path="reason"),
            ],
        ),
    )
    return ConditionStep(
        name="Gate",
        conditions=[
            ConditionGreaterThanOrEqualTo(
                left=JsonGet(
                    step_name=gate_step.name, property_file=gate_file, json_path="passed_int"
                ),
                right=1,
            )
        ],
        if_steps=[register],
        else_steps=[failed],
    )


def triage_pipeline(config: SageMakerConfig, session: Any | None = None) -> Any:
    """Project 1: DataCheck, Train, Evaluate, Gate (Register or GateFailed)."""
    from sagemaker.core.workflow.properties import PropertyFile
    from sagemaker.mlops.workflow.pipeline import Pipeline

    session = session or fake_session(config.region, config.bucket)
    params = _parameters("triage", config.defaults)
    names = BY_PIPELINE["triage"]
    artifacts = _artifacts_uri(params, "triage")
    data_in = ("data", params["data_uri"], DATA_DIR)
    check = _processing_step(
        "DataCheck",
        "nw.pipelines.steps.triage_data_check",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS],
        inputs=[data_in],
        artifacts_out=artifacts,
    )
    train = _processing_step(
        "Train",
        "nw.pipelines.steps.triage_train",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS, "--package-dir", ARTIFACTS]
        + _flags(params, names, "target_recall", "min_precision", "seed"),
        inputs=[data_in],
        artifacts_out=artifacts,
        depends_on=[check],
    )
    gate_file = PropertyFile(
        name="TriageGate", output_name="artifacts", path="steps/triage_evaluate.json"
    )
    evaluate = _processing_step(
        "Evaluate",
        "nw.pipelines.steps.triage_evaluate",
        config,
        session,
        arguments=["--out", ARTIFACTS, "--production-summary", PRODUCTION_DIR]
        + _flags(
            params,
            names,
            "min_p0_recall",
            "max_ece",
            "max_macro_f1_drop",
            "max_brier_increase",
            "max_p0_recall_drop",
            "force",
        ),
        inputs=[
            ("artifacts", _artifacts_of(train), ARTIFACTS),
            ("production", params["production_summary"], PRODUCTION_DIR),
        ],
        artifacts_out=artifacts,
        property_files=[gate_file],
    )
    gate = _register_and_gate(
        "triage",
        config,
        session,
        trigger=params["trigger"],
        gate_step=evaluate,
        gate_file=gate_file,
        model_artifacts_uri=_artifacts_of(train),
    )
    return Pipeline(
        name=config.pipeline_name("triage"),
        parameters=list(params.values()),
        steps=[check, train, evaluate, gate],
        sagemaker_session=session,
    )


def semantic_pipeline(config: SageMakerConfig, session: Any | None = None) -> Any:
    """Project 2: DataPrep, Train, Export, Benchmark, GateCheck, Gate (Register or GateFailed)."""
    from sagemaker.core.workflow.properties import PropertyFile
    from sagemaker.mlops.workflow.pipeline import Pipeline

    session = session or fake_session(config.region, config.bucket)
    params = _parameters("semantic", config.defaults)
    names = BY_PIPELINE["semantic"]
    artifacts = _artifacts_uri(params, "semantic")
    data_in = ("data", params["data_uri"], DATA_DIR)
    prep = _processing_step(
        "DataPrep",
        "nw.pipelines.steps.semantic_data_prep",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS],
        inputs=[data_in],
        artifacts_out=artifacts,
    )
    train = _processing_step(
        "Train",
        "nw.pipelines.steps.semantic_train",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS]
        + _flags(params, names, "epochs", "subset", "lr", "batch_size", "seed"),
        inputs=[data_in],
        artifacts_out=artifacts,
        instance_type=config.training_instance_type,
        depends_on=[prep],
    )
    export = _processing_step(
        "Export",
        "nw.pipelines.steps.semantic_export",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS, "--package-dir", ARTIFACTS]
        + _flags(params, names, "parity_n"),
        inputs=[data_in, ("artifacts", _artifacts_of(train), ARTIFACTS)],
        artifacts_out=artifacts,
    )
    benchmark = _processing_step(
        "Benchmark",
        "nw.pipelines.steps.semantic_benchmark",
        config,
        session,
        arguments=["--data", DATA_DIR, "--out", ARTIFACTS]
        + _flags(params, names, "latency_n", "triage_artifact"),
        inputs=[data_in, ("artifacts", _artifacts_of(export), ARTIFACTS)],
        artifacts_out=artifacts,
    )
    gate_file = PropertyFile(
        name="SemanticGate", output_name="artifacts", path="steps/semantic_gate.json"
    )
    gate_check = _processing_step(
        "GateCheck",
        "nw.pipelines.steps.semantic_gate",
        config,
        session,
        arguments=["--out", ARTIFACTS, "--production-summary", PRODUCTION_DIR]
        + _flags(
            params,
            names,
            "min_p0_recall",
            "min_tag_micro_f1",
            "max_int8_p95_ms",
            "max_tag_micro_f1_drop",
            "max_priority_macro_f1_drop",
            "force",
        ),
        inputs=[
            ("artifacts", _artifacts_of(benchmark), ARTIFACTS),
            ("production", params["production_summary"], PRODUCTION_DIR),
        ],
        artifacts_out=artifacts,
        property_files=[gate_file],
    )
    gate = _register_and_gate(
        "semantic",
        config,
        session,
        trigger=params["trigger"],
        gate_step=gate_check,
        gate_file=gate_file,
        model_artifacts_uri=_artifacts_of(export),
    )
    return Pipeline(
        name=config.pipeline_name("semantic"),
        parameters=list(params.values()),
        steps=[prep, train, export, benchmark, gate_check, gate],
        sagemaker_session=session,
    )


FACTORIES = {"triage": triage_pipeline, "semantic": semantic_pipeline}


def definition(
    pipeline: str, config: SageMakerConfig, session: Any | None = None
) -> dict[str, Any]:
    """The pipeline definition as the service receives it, built without an AWS call."""
    return json.loads(FACTORIES[pipeline](config, session).definition())
