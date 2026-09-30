"""The Azure Machine Learning pipeline graphs. SDK imports are inside the functions so the
module loads without `azure-ai-ml` and the tests skip cleanly when the extra is absent.

Every step is a command component running the course's nw-pipelines image. Azure ML gives
each step its own output folder, while the shared steps expect one artifact tree, so every
step after the first copies its predecessor's tree into its own output before it runs (a
few megabytes for Project 1, the ONNX files for Project 2). The last node's output is the
run's whole tree, written under `output_root` when that is an `azureml://` path.

Every step runs the launcher (`nw.pipelines.source`) on the optional input `source`: the source
bundle of the checkout that submitted the run, which `AzureMLPipelineRunner.submit` builds and
passes as the job input `source_uri` (uploaded with the submission, like a local data file). So
the steps run the learner's `nw/`, and the image supplies only the dependencies.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from nw.pipelines.params import BY_PIPELINE, Param
from nw.platform.base import Tenant

# Parameters that are files: a local path (uploaded at submission), or an `azureml://`
# datastore URI on the platform.
FILE_PARAMS = ("data_uri", "production_summary", "source_uri")
# File parameters a run may leave out (the bundle is set by the platform client on submit).
OPTIONAL_FILES = ("source_uri",)
# Parameters whose flag is omitted when the value is empty, as the steps expect.
OPTIONAL_FLAGS = ("base", "max_length", "triage_artifact")
LAUNCH = "python -m nw.pipelines.source run $[[--source ${{inputs.source}}]] --"
# Parameters whose step flag is not the parameter's name.
FLAG_NAMES = {"parity_n": "--n", "triage_artifact": "--triage"}
COPY = "cp -R ${{inputs.tree}}/. ${{outputs.artifacts}}/ && "
GATE_STEP = {"triage": "triage_evaluate", "semantic": "semantic_gate"}
REGISTRY_FACTORY = "nw.platform.azure:registry_from_env"


@dataclass(frozen=True)
class AzureMLConfig:
    """What a definition needs beyond its parameters. Nothing here is a secret."""

    tenant: Tenant
    image: str  # the nw-pipelines image in ACR
    compute: str = "serverless"
    instance_type: str = "Standard_DS3_v2"
    training_instance_type: str = "Standard_D8s_v3"
    # The user-assigned identity the steps run as (the register step needs it to reach the
    # registry); unset leaves the workspace default.
    identity_client_id: str | None = None
    # Settings the register step's platform client reads (`NW_AZURE_*`), passed as environment.
    platform_env: Mapping[str, str] = field(default_factory=dict)
    # Parameter defaults that replace the repo paths of `nw.pipelines.params` (the tickets and
    # the production summaries on the workspace datastore, the tenant's run tree).
    defaults: Mapping[str, Any] = field(default_factory=dict)

    def experiment(self, pipeline: str) -> str:
        """`northwind-alice-triage`: the experiment every run of the tenant's pipeline joins."""
        return self.tenant.resource(pipeline)


def _signature(pipeline: str) -> inspect.Signature:
    """The pipeline function's signature, generated from `nw.pipelines.params` so the Azure ML
    inputs and the other definitions cannot drift: the SDK reads inputs from the signature."""
    from azure.ai.ml import Input

    out = []
    for p in BY_PIPELINE[pipeline]:
        if p.name in FILE_PARAMS:
            annotation: Any = Input(type="uri_file", optional=p.name in OPTIONAL_FILES)
            default: Any = None
        else:
            annotation, default = p.kind, p.default
        out.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )
    return inspect.Signature(out)


def _types(params: tuple[Param, ...], names: tuple[str, ...]) -> dict[str, Any]:
    from azure.ai.ml import Input

    kinds = {bool: "boolean", int: "integer", float: "number", str: "string"}
    by_name = {p.name: p for p in params}
    out: dict[str, Any] = {}
    for n in names:
        if n in FILE_PARAMS:
            out[n] = Input(type="uri_file")
        else:
            out[n] = Input(type=kinds[by_name[n].kind], optional=n in OPTIONAL_FLAGS)
    return out


def _flags(names: tuple[str, ...]) -> str:
    """`--min-p0-recall ${{inputs.min_p0_recall}}`, optional flags inside `$[[...]]`."""
    parts = []
    for n in names:
        flag = FLAG_NAMES.get(n) or f"--{n.replace('_', '-')}"
        text = f"{flag} ${{{{inputs.{n}}}}}"
        parts.append(f"$[[{text}]]" if n in OPTIONAL_FLAGS else text)
    return " ".join(parts)


def _component(
    config: AzureMLConfig,
    pipeline: str,
    name: str,
    module: str,
    *,
    args: str,
    params: tuple[str, ...] = (),
    tree: bool = True,
    data: bool = False,
    training: bool = False,
    extra_env: Mapping[str, str] | None = None,
    body: str | None = None,
) -> Any:
    from azure.ai.ml import Input, Output, command
    from azure.ai.ml.entities import Environment

    inputs: dict[str, Any] = _types(BY_PIPELINE[pipeline], params)
    if tree:
        inputs["tree"] = Input(type="uri_folder")
    if data:
        inputs["data"] = Input(type="uri_file")
    inputs["source"] = Input(type="uri_file", optional=True)
    run = body or f"{LAUNCH} {module} {args} {_flags(params)}".strip()
    env = {
        "NW_TRACK": "azure",
        "NW_TENANT": config.tenant.name,
        "NW_ENVIRONMENT": config.tenant.environment,
        "TOKENIZERS_PARALLELISM": "false",
        **(extra_env or {}),
    }
    if config.identity_client_id:
        env["AZURE_CLIENT_ID"] = config.identity_client_id
    return command(
        name=name,
        display_name=name,
        command=(COPY if tree else "") + run,
        inputs=inputs,
        outputs={"artifacts": Output(type="uri_folder")},
        environment=Environment(image=config.image),
        environment_variables=env,
        instance_type=config.training_instance_type if training else config.instance_type,
        # Never reuse a previous run's output: every run trains and gates afresh.
        is_deterministic=False,
    )


def _tenant_flags(config: AzureMLConfig) -> str:
    return f"--tenant {config.tenant.name} --environment {config.tenant.environment}"


def _register(config: AzureMLConfig, pipeline: str) -> Any:
    """Registers the candidate that cleared the gate through `nw.platform.azure`'s registry
    (`NW_PIPELINE_REGISTRY`), a candidate tagged with the gate, the git and data hashes and the
    trigger. `register_model=false` records the decision and registers nothing."""
    step = (
        f"{LAUNCH} nw.pipelines.steps.register"
        f" --pipeline {pipeline} --out ${{{{outputs.artifacts}}}}"
        f" --tenant {config.tenant.name} --environment {config.tenant.environment}"
        " --trigger ${{inputs.trigger}}"
    )
    body = (
        'case "${{inputs.register_model}}" in '
        f"[Tt]rue) {step} ;; "
        f'*) echo "registration disabled for this run" ;; esac'
    )
    return _component(
        config,
        pipeline,
        "register",
        "nw.pipelines.steps.register",
        args="",
        params=("register_model", "trigger"),
        extra_env={"NW_PIPELINE_REGISTRY": REGISTRY_FACTORY, **config.platform_env},
        body=body,
    )


def _finish(job: Any, config: AzureMLConfig, pipeline: str, values: Mapping[str, Any]) -> Any:
    from azure.ai.ml import Output
    from azure.ai.ml.entities import ManagedIdentityConfiguration

    root = str(values.get("output_root") or "")
    if root.startswith("azureml://"):
        job.outputs.artifacts = Output(
            type="uri_folder", mode="rw_mount", path=f"{root.rstrip('/')}/{pipeline}/${{{{name}}}}/"
        )
    job.settings.default_compute = config.compute
    job.settings.continue_on_step_failure = False
    job.experiment_name = config.experiment(pipeline)
    job.display_name = f"{config.tenant.resource(pipeline)}-{values.get('trigger', 'manual')}"
    job.tags = {
        "tenant": config.tenant.name,
        "environment": config.tenant.environment,
        "pipeline": pipeline,
        "trigger": str(values.get("trigger", "manual")),
    }
    if config.identity_client_id:
        job.identity = ManagedIdentityConfiguration(client_id=config.identity_client_id)
    return job


def _build(
    pipeline: str,
    config: AzureMLConfig,
    graph: Callable[[dict[str, Any], dict[str, Any]], Any],
    params: Mapping[str, Any] | None,
) -> Any:
    from azure.ai.ml import Input
    from azure.ai.ml.dsl import pipeline as dsl_pipeline

    values = {p.name: p.default for p in BY_PIPELINE[pipeline]}
    values.update({k: v for k, v in config.defaults.items() if k in values})
    values.update({k: v for k, v in (params or {}).items() if k in values})
    values["tenant"], values["environment"] = config.tenant.name, config.tenant.environment
    for name in FILE_PARAMS:
        if name in OPTIONAL_FILES and not values.get(name):
            values[name] = None
            continue
        values[name] = Input(type="uri_file", path=str(values[name]))

    def body(**inputs: Any) -> Any:
        return graph(inputs, values)

    sig = _signature(pipeline)
    body.__signature__ = sig  # type: ignore[attr-defined]
    body.__annotations__ = {n: q.annotation for n, q in sig.parameters.items()}
    body.__name__ = f"northwind_{pipeline}"
    fn = dsl_pipeline(
        name=f"northwind-{pipeline}",
        description=DESCRIPTIONS[pipeline],
        default_compute=config.compute,
    )(body)
    return _finish(fn(**values), config, pipeline, values)


def _pick(inputs: Mapping[str, Any], *names: str) -> dict[str, Any]:
    return {n: inputs[n] for n in names}


def _optional(inputs: Mapping[str, Any], values: Mapping[str, Any], *names: str) -> dict[str, Any]:
    """Optional inputs are wired only when the run sets them, so `$[[...]]` drops the flag."""
    return {n: inputs[n] for n in names if values.get(n)}


def _source(inputs: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """The bundle as every step's `source` input, when the run has one."""
    return {"source": inputs["source_uri"]} if values.get("source_uri") else {}


def triage_pipeline(config: AzureMLConfig, params: Mapping[str, Any] | None = None) -> Any:
    """Project 1: data check, train, evaluate through the gate, register."""
    check_c = _component(
        config,
        "triage",
        "data_check",
        "nw.pipelines.steps.triage_data_check",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        tree=False,
        data=True,
    )
    train_c = _component(
        config,
        "triage",
        "train",
        "nw.pipelines.steps.triage_train",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        params=("target_recall", "min_precision", "seed"),
        data=True,
    )
    evaluate_c = _component(
        config,
        "triage",
        "evaluate",
        "nw.pipelines.steps.triage_evaluate",
        args=f"--out ${{{{outputs.artifacts}}}} {_tenant_flags(config)}",
        params=(
            "production_summary",
            "min_p0_recall",
            "max_ece",
            "max_macro_f1_drop",
            "max_brier_increase",
            "max_p0_recall_drop",
            "force",
            "champion",
        ),
        extra_env={"NW_PIPELINE_REGISTRY": REGISTRY_FACTORY, **config.platform_env},
    )
    register_c = _register(config, "triage")

    def graph(i: dict[str, Any], values: dict[str, Any]) -> Any:
        src = _source(i, values)
        data_check = check_c(data=i["data_uri"], **src)
        train = train_c(
            tree=data_check.outputs.artifacts,
            data=i["data_uri"],
            **_pick(i, "target_recall", "min_precision", "seed"),
            **src,
        )
        evaluate = evaluate_c(
            tree=train.outputs.artifacts,
            **_pick(
                i,
                "production_summary",
                "min_p0_recall",
                "max_ece",
                "max_macro_f1_drop",
                "max_brier_increase",
                "max_p0_recall_drop",
                "force",
                "champion",
            ),
            **src,
        )
        register = register_c(
            tree=evaluate.outputs.artifacts, **_pick(i, "register_model", "trigger"), **src
        )
        return {"artifacts": register.outputs.artifacts}

    return _build("triage", config, graph, params)


def semantic_pipeline(config: AzureMLConfig, params: Mapping[str, Any] | None = None) -> Any:
    """Project 2: data prep, train, export, benchmark, gate, register."""
    prep_c = _component(
        config,
        "semantic",
        "data_prep",
        "nw.pipelines.steps.semantic_data_prep",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        tree=False,
        data=True,
    )
    train_c = _component(
        config,
        "semantic",
        "train",
        "nw.pipelines.steps.semantic_train",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        params=("epochs", "subset", "lr", "batch_size", "seed", "base", "max_length"),
        data=True,
        training=True,
    )
    export_c = _component(
        config,
        "semantic",
        "export",
        "nw.pipelines.steps.semantic_export",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        params=("parity_n",),
        data=True,
    )
    benchmark_c = _component(
        config,
        "semantic",
        "benchmark",
        "nw.pipelines.steps.semantic_benchmark",
        args="--data ${{inputs.data}} --out ${{outputs.artifacts}}",
        params=("latency_n", "triage_artifact"),
        data=True,
    )
    gate_c = _component(
        config,
        "semantic",
        "gate",
        "nw.pipelines.steps.semantic_gate",
        args=f"--out ${{{{outputs.artifacts}}}} {_tenant_flags(config)}",
        params=(
            "production_summary",
            "min_p0_recall",
            "min_tag_micro_f1",
            "max_int8_p95_ms",
            "max_tag_micro_f1_drop",
            "max_priority_macro_f1_drop",
            "force",
            "champion",
        ),
        extra_env={"NW_PIPELINE_REGISTRY": REGISTRY_FACTORY, **config.platform_env},
    )
    register_c = _register(config, "semantic")

    def graph(i: dict[str, Any], values: dict[str, Any]) -> Any:
        src = _source(i, values)
        data_prep = prep_c(data=i["data_uri"], **src)
        train = train_c(
            tree=data_prep.outputs.artifacts,
            data=i["data_uri"],
            **_pick(i, "epochs", "subset", "lr", "batch_size", "seed"),
            **_optional(i, values, "base", "max_length"),
            **src,
        )
        export = export_c(
            tree=train.outputs.artifacts, data=i["data_uri"], parity_n=i["parity_n"], **src
        )
        benchmark = benchmark_c(
            tree=export.outputs.artifacts,
            data=i["data_uri"],
            latency_n=i["latency_n"],
            **_optional(i, values, "triage_artifact"),
            **src,
        )
        gate = gate_c(
            tree=benchmark.outputs.artifacts,
            **_pick(
                i,
                "production_summary",
                "min_p0_recall",
                "min_tag_micro_f1",
                "max_int8_p95_ms",
                "max_tag_micro_f1_drop",
                "max_priority_macro_f1_drop",
                "force",
                "champion",
            ),
            **src,
        )
        register = register_c(
            tree=gate.outputs.artifacts, **_pick(i, "register_model", "trigger"), **src
        )
        return {"artifacts": register.outputs.artifacts}

    return _build("semantic", config, graph, params)


DESCRIPTIONS = {
    "triage": "Northwind ticket triage: data contract, train, gate, register.",
    "semantic": "Northwind semantic understanding: prep, fine-tune, export, benchmark, gate, "
    "register.",
}
FACTORIES = {"triage": triage_pipeline, "semantic": semantic_pipeline}


def definition(
    pipeline: str, config: AzureMLConfig, params: Mapping[str, Any] | None = None
) -> Any:
    """The `PipelineJob` for `pipeline` (`triage`, `semantic`, or a `retrain-` alias), built
    locally without an Azure call; `MLClient.jobs.create_or_update` submits it."""
    from nw.pipelines import canonical

    return FACTORIES[canonical(pipeline)](config, params)


def as_dict(job: Any) -> dict[str, Any]:
    """The job as the YAML `az ml job create` accepts, for review and tests."""
    return job._to_dict()
