"""The training pipelines for Project 1 and Project 2 (ADR 0008, ADR 0011).

One set of step functions, two pipeline definitions:

- `nw.pipelines.steps`: plain functions with file paths in and out, one module per step, each
  runnable as `python -m nw.pipelines.steps.<name>`. This is the only code that trains,
  evaluates or registers anything.
- `nw.pipelines.kfp`: the Kubeflow Pipelines SDK v2 definitions. Compiled YAML runs on the
  Kubeflow local runner (Local track) and on Vertex AI Pipelines (Google Cloud track).
- `nw.pipelines.sagemaker`: the SageMaker Pipelines definitions (AWS track), a ProcessingStep
  per step, a ConditionStep on the gate and a register step into the tenant's model package
  group with `PendingManualApproval`.
- `nw.pipelines.retrain`: submit either pipeline through the active track's `PipelineRunner`
  with the same parameters the weekly retraining jobs pass.

Every step writes its result as JSON so a step's outcome is a file, never a return value
the orchestrator has to marshal. The gate decision lives in `steps/<gate>.json` under the
artifact root and carries `passed_int`, which is what a SageMaker ConditionStep can compare.
"""

from __future__ import annotations

from pathlib import Path

PIPELINES = ("triage", "semantic")
# The weekly jobs name the same pipelines by what they do: the Cloud Scheduler job reads
# `<prefix>/pipelines/retrain-triage.yaml`, so the compiler writes each pipeline under both names.
ALIASES = {"retrain-triage": "triage", "retrain-semantic": "semantic"}
NAMES = PIPELINES + tuple(ALIASES)
DEFAULT_IMAGE = "nw-pipelines:latest"
COMPILED_DIR = Path("artifacts/pipelines")


def canonical(name: str) -> str:
    """`retrain-triage` to `triage`; a canonical name to itself; anything else raises."""
    name = ALIASES.get(name, name)
    if name not in PIPELINES:
        raise ValueError(f"unknown pipeline {name!r}; expected one of {', '.join(NAMES)}")
    return name


def aliases_of(name: str) -> list[str]:
    return [alias for alias, target in ALIASES.items() if target == name]


__all__ = [
    "ALIASES",
    "COMPILED_DIR",
    "DEFAULT_IMAGE",
    "NAMES",
    "PIPELINES",
    "aliases_of",
    "canonical",
]
