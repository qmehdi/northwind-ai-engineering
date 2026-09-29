"""Azure Machine Learning pipeline definitions: the Azure track (ADR 0013).

    uv run python -m nw.pipelines.azureml --pipeline triage --tenant alice \\
        --image northwindacr.azurecr.io/nw-pipelines:latest

The same steps as the Kubeflow and SageMaker definitions, each a command component running the
course image with `python -m nw.pipelines.steps.<name>`: data check (or prep), train, evaluate
(or export, benchmark and gate), then a register step that refuses a candidate whose gate
failed and registers one that passed in the tenant's Azure ML model as a `candidate`, tagged
with the gate, the hashes and the trigger. Parameters are generated from
`nw.pipelines.params`, so the three definitions cannot drift. `definition()` builds the
`PipelineJob` locally with the SDK, without an Azure call; `nw.platform.azure` submits it.

Verified against `azure-ai-ml` 1.35.0 (`azure.ai.ml.command`, `azure.ai.ml.dsl.pipeline`,
serverless compute through `default_compute="serverless"`, optional inputs with `$[[...]]`),
docs at learn.microsoft.com/azure/machine-learning/how-to-create-component-pipeline-python,
fetched 2026-09-29.
"""

from __future__ import annotations

from nw.pipelines.azureml.definitions import (
    AzureMLConfig,
    as_dict,
    definition,
    semantic_pipeline,
    triage_pipeline,
)

__all__ = ["AzureMLConfig", "as_dict", "definition", "semantic_pipeline", "triage_pipeline"]
