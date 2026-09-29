"""The two pipelines as `@dsl.pipeline` graphs over the components, and their compiler.

The signatures repeat `nw.pipelines.params` on purpose: the SDK derives the pipeline's
parameters from the function signature, and a test keeps the two in step.

No `from __future__ import annotations` here: the SDK reads the real annotations.
"""

import shutil
from pathlib import Path

from kfp import compiler, dsl

from nw.pipelines import COMPILED_DIR, DEFAULT_IMAGE, aliases_of, canonical
from nw.pipelines.kfp.components import build

PIPELINE_NAMES = {"triage": "northwind-triage", "semantic": "northwind-semantic"}


def triage_pipeline(image: str = DEFAULT_IMAGE):
    """Project 1: data check, train, evaluate through the gate, register."""
    c = build(image)

    @dsl.pipeline(
        name=PIPELINE_NAMES["triage"],
        description="Northwind ticket triage: data contract, train, gate, register.",
    )
    def pipeline(
        data_uri: str = "data/tickets.jsonl",
        output_root: str = "artifacts/pipelines/run",
        tenant: str = "solo",
        environment: str = "northwind",
        force: bool = False,
        register_model: bool = True,
        trigger: str = "manual",
        production_summary: str = "data/golden/triage_production.json",
        target_recall: float = 0.90,
        min_precision: float = 0.25,
        seed: int = 0,
        min_p0_recall: float = 0.85,
        max_ece: float = 0.12,
        max_macro_f1_drop: float = 0.02,
        max_brier_increase: float = 0.01,
        max_p0_recall_drop: float = 0.03,
    ) -> str:
        check = c["triage_data_check"](data_uri=data_uri, output_root=output_root)
        check.set_caching_options(False)
        train = c["triage_train"](
            data_uri=data_uri,
            output_root=output_root,
            target_recall=target_recall,
            min_precision=min_precision,
            seed=seed,
        )
        train.set_caching_options(False).after(check)
        evaluate = c["triage_evaluate"](
            output_root=output_root,
            version=train.outputs["Output"],
            production_summary=production_summary,
            min_p0_recall=min_p0_recall,
            max_ece=max_ece,
            max_macro_f1_drop=max_macro_f1_drop,
            max_brier_increase=max_brier_increase,
            max_p0_recall_drop=max_p0_recall_drop,
            force=force,
        )
        evaluate.set_caching_options(False)
        registered = c["register"](
            pipeline="triage",
            output_root=output_root,
            version=train.outputs["Output"],
            passed=evaluate.outputs["Output"],
            tenant=tenant,
            environment=environment,
            enabled=register_model,
            trigger=trigger,
        )
        registered.set_caching_options(False)
        return registered.output

    return pipeline


def semantic_pipeline(image: str = DEFAULT_IMAGE):
    """Project 2: data prep, train, export, benchmark, gate, register."""
    c = build(image)

    @dsl.pipeline(
        name=PIPELINE_NAMES["semantic"],
        description="Northwind semantic understanding: prep, fine-tune, export, benchmark, "
        "gate, register.",
    )
    def pipeline(
        data_uri: str = "data/tickets.jsonl",
        output_root: str = "artifacts/pipelines/run",
        tenant: str = "solo",
        environment: str = "northwind",
        force: bool = False,
        register_model: bool = True,
        trigger: str = "manual",
        production_summary: str = "data/golden/semantic_production.json",
        epochs: int = 2,
        subset: int = 2000,
        lr: float = 1e-3,
        batch_size: int = 16,
        seed: int = 0,
        base: str = "",
        max_length: int = 0,
        triage_artifact: str = "",
        parity_n: int = 20,
        latency_n: int = 100,
        min_p0_recall: float = 0.70,
        min_tag_micro_f1: float = 0.50,
        max_int8_p95_ms: float = 100.0,
        max_tag_micro_f1_drop: float = 0.02,
        max_priority_macro_f1_drop: float = 0.02,
    ) -> str:
        prep = c["semantic_data_prep"](data_uri=data_uri, output_root=output_root)
        prep.set_caching_options(False)
        train = c["semantic_train"](
            data_uri=data_uri,
            output_root=output_root,
            epochs=epochs,
            subset=subset,
            lr=lr,
            batch_size=batch_size,
            seed=seed,
            base=base,
            max_length=max_length,
        )
        train.set_caching_options(False).after(prep)
        version = train.outputs["Output"]
        export = c["semantic_export"](
            data_uri=data_uri, output_root=output_root, version=version, parity_n=parity_n
        )
        export.set_caching_options(False)
        bench = c["semantic_benchmark"](
            data_uri=data_uri,
            output_root=output_root,
            version=version,
            triage_artifact=triage_artifact,
            latency_n=latency_n,
        )
        bench.set_caching_options(False).after(export)
        gate = c["semantic_gate"](
            output_root=output_root,
            version=version,
            production_summary=production_summary,
            min_p0_recall=min_p0_recall,
            min_tag_micro_f1=min_tag_micro_f1,
            max_int8_p95_ms=max_int8_p95_ms,
            max_tag_micro_f1_drop=max_tag_micro_f1_drop,
            max_priority_macro_f1_drop=max_priority_macro_f1_drop,
            force=force,
        )
        gate.set_caching_options(False).after(bench)
        registered = c["register"](
            pipeline="semantic",
            output_root=output_root,
            version=version,
            passed=gate.outputs["Output"],
            tenant=tenant,
            environment=environment,
            enabled=register_model,
            trigger=trigger,
        )
        registered.set_caching_options(False)
        return registered.output

    return pipeline


FACTORIES = {"triage": triage_pipeline, "semantic": semantic_pipeline}


def compile_one(name: str, out_dir: Path = COMPILED_DIR, image: str = DEFAULT_IMAGE) -> Path:
    """`<out_dir>/<name>.yaml`, plus the same bytes under each scheduler alias
    (`retrain-triage.yaml`), so a weekly job and a hand submission read one definition."""
    name = canonical(name)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.yaml"
    compiler.Compiler().compile(FACTORIES[name](image), str(target))
    for alias in aliases_of(name):
        shutil.copyfile(target, out_dir / f"{alias}.yaml")
    return target


def compile_all(out_dir: Path = COMPILED_DIR, image: str = DEFAULT_IMAGE) -> dict[str, Path]:
    """`artifacts/pipelines/triage.yaml` and `semantic.yaml` (and their `retrain-` copies),
    what Vertex and the local runner both accept."""
    return {name: compile_one(name, out_dir, image) for name in FACTORIES}
