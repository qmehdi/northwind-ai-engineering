"""The parameters of both pipelines, declared once.

The Kubeflow definitions repeat them as function signatures (the SDK reads the signature),
the SageMaker definitions turn them into `Parameter*` objects, `nw.pipelines.retrain` turns
them into command line flags, and a test checks the three agree. The names are the ones the
retraining workflows already use (`epochs`, `force`), plus the gate bars from the two
`GatePolicy` classes, so a weekly job can tighten a bar without a code change, and `trigger`,
which tells a scheduled run from a hand submission on the registered version.

`source_uri` is the bundle of the submitting checkout's `nw/` (`nw.pipelines.source`), which the
cloud platform clients set on every submit so the steps run the learner's code. `champion`
picks what the gate compares against (`nw.pipelines.champion`).

The defaults of `data_uri`, `output_root` and `production_summary` are repo paths, right for the
Local track. The platform clients replace them with the deployed locations: the tickets in the
data bucket, the tenant's prefix in the artifacts bucket, and the production summaries the
deploy copies to `<artifacts>/baselines/` (`baseline_uri`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nw.semantic.promote import GatePolicy as SemanticPolicy
from nw.triage.promote import GatePolicy as TriagePolicy


@dataclass(frozen=True)
class Param:
    name: str
    default: Any
    help: str
    # Baked into the SageMaker definition instead of being a pipeline parameter: the tenant
    # is in the model package group name, and registration is the condition step.
    sagemaker: bool = True

    @property
    def kind(self) -> type:
        return type(self.default)

    @property
    def sagemaker_name(self) -> str:
        """`min_p0_recall` becomes `MinP0Recall`, what the SageMaker SDK expects."""
        return "".join(part.capitalize() for part in self.name.split("_"))


_TP = TriagePolicy()
_SP = SemanticPolicy()

COMMON: tuple[Param, ...] = (
    Param("data_uri", "data/tickets.jsonl", "the ticket file: a path, gs:// or s3:// URI"),
    Param("output_root", "artifacts/pipelines/run", "where the run writes its artifact tree"),
    Param("tenant", "solo", "the learner's tenant name (NW_TENANT)", sagemaker=False),
    Param("environment", "northwind", "the environment name (NW_ENVIRONMENT)", sagemaker=False),
    Param("force", False, "pass the gate anyway, recorded as forced"),
    Param("register_model", True, "register the candidate that clears the gate", sagemaker=False),
    # For the record only: no step reads it to decide anything. The weekly jobs pass
    # `schedule`, a hand submission keeps `manual`, and the registered version carries it.
    Param("trigger", "manual", "who started the run: manual or schedule, recorded on the version"),
    # The learner's code (nw.pipelines.source): the cloud clients build, upload and set it on
    # every submit; empty runs the code the step finds (the checkout on the Local track).
    Param("source_uri", "", "the source bundle of nw/ the steps run; set by the platform client"),
    # NW_* settings the steps that reach the registry need (the gate's champion lookup and the
    # register step), as a JSON object; Vertex runs have no other way to learn their platform.
    Param(
        "platform_env",
        "",
        "JSON object of NW_* settings for the steps that reach the registry",
        sagemaker=False,
    ),
    Param(
        "champion",
        "registry",
        "what the gate compares against: registry (the live version, else the summary) or summary",
    ),
)

TRIAGE: tuple[Param, ...] = COMMON + (
    Param(
        "production_summary",
        "data/golden/triage_production.json",
        "the committed production summary the gate compares against",
    ),
    Param("target_recall", 0.90, "P0 recall the threshold is chosen for on validation"),
    Param("min_precision", 0.25, "P0 precision floor while choosing the threshold"),
    Param("seed", 0, "random seed"),
    Param("min_p0_recall", _TP.min_p0_recall, "gate: P0 recall floor on the test split"),
    Param("max_ece", _TP.max_ece, "gate: expected calibration error ceiling"),
    Param("max_macro_f1_drop", _TP.max_macro_f1_drop, "gate: macro-F1 drop against production"),
    Param("max_brier_increase", _TP.max_brier_increase, "gate: Brier increase against production"),
    Param("max_p0_recall_drop", _TP.max_p0_recall_drop, "gate: P0 recall drop against production"),
)

SEMANTIC: tuple[Param, ...] = COMMON + (
    Param(
        "production_summary",
        "data/golden/semantic_production.json",
        "the committed production summary the gate compares against",
    ),
    Param("epochs", 2, "training epochs on the subset"),
    Param("subset", 2000, "stratified training subset; 0 means the whole training split"),
    Param("lr", 1e-3, "learning rate"),
    Param("batch_size", 16, "batch size"),
    Param("seed", 0, "random seed"),
    Param("base", "", "base encoder; empty means the course default"),
    Param("max_length", 0, "token limit; 0 means the base default"),
    Param("triage_artifact", "", "a Project 1 artifact for the benchmark; empty trains one"),
    Param("parity_n", 20, "held-out tickets the export parity check uses"),
    Param("latency_n", 100, "tickets the benchmark times"),
    Param("min_p0_recall", _SP.min_p0_recall, "gate: P0 recall floor on the test split"),
    Param("min_tag_micro_f1", _SP.min_tag_micro_f1, "gate: tag micro-F1 floor"),
    Param("max_int8_p95_ms", _SP.max_int8_p95_ms, "gate: int8 p95 latency ceiling in ms"),
    Param(
        "max_tag_micro_f1_drop", _SP.max_tag_micro_f1_drop, "gate: tag micro-F1 drop vs production"
    ),
    Param(
        "max_priority_macro_f1_drop",
        _SP.max_priority_macro_f1_drop,
        "gate: priority macro-F1 drop vs production",
    ),
)

BY_PIPELINE: dict[str, tuple[Param, ...]] = {"triage": TRIAGE, "semantic": SEMANTIC}


def defaults(pipeline: str) -> dict[str, Any]:
    return {p.name: p.default for p in BY_PIPELINE[pipeline]}


BASELINES = "baselines"


def baseline_uri(scheme: str, bucket: str, pipeline: str) -> str:
    """`s3://<bucket>/baselines/triage_production.json`: where the deploy puts the committed
    production summary of `pipeline` (CDK `BucketDeployment`, Terraform bucket objects)."""
    return f"{scheme}://{bucket}/{BASELINES}/{pipeline}_production.json"


def sagemaker_parameter_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


__all__ = [
    "BASELINES",
    "BY_PIPELINE",
    "SEMANTIC",
    "TRIAGE",
    "Param",
    "baseline_uri",
    "defaults",
    "sagemaker_parameter_name",
]
