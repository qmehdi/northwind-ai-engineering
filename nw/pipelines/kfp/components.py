"""The Kubeflow components, one per step, built for one image.

The bodies are deliberately self-contained: the SDK serialises a lightweight component's
source and runs it in `base_image`, so a body can import only what that image has, and
nothing from this module. Each body runs the step module as a subprocess with the pipeline's
parameters as flags, then reads `steps/<name>.json` for what the next component needs.

The subprocess is the launcher `python -m nw.pipelines.source run --source <source_uri> --
<module>`: with a `source_uri` (every Vertex submit sets one) the step runs the learner's `nw/`
from the bundle, not the image's copy; `platform_env` (a JSON object of `NW_*` settings the
Vertex submit sets) tells the gate's champion lookup and the register step which platform
they are on. On the Local track both are empty and the steps run the checkout.
An `output_root` of `gs://<bucket>/...` means `/gcs/<bucket>/...`, where Vertex AI Pipelines
mounts the bucket, so every step of a Vertex run shares one artifact tree.

No `from __future__ import annotations` here: the SDK reads the real annotations.
"""

from collections.abc import Callable

from kfp import dsl


def build(image: str) -> dict[str, Callable]:
    """The components for `image`, keyed by step name."""

    @dsl.component(base_image=image, install_kfp_package=False)
    def triage_data_check(
        data_uri: str,
        output_root: str,
        profile: dsl.Output[dsl.Metrics],
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/triage"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch + ["nw.pipelines.steps.triage_data_check"] + ["--data", data_uri, "--out", out],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "triage_data_check.json").read_text())
        profile.log_metric("rows", float(result["n"]))
        for split, n in result["splits"].items():
            profile.log_metric(f"rows_{split}", float(n))
        return str(result["data_sha256_12"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def triage_train(
        data_uri: str,
        output_root: str,
        metrics: dsl.Output[dsl.Metrics],
        target_recall: float = 0.90,
        min_precision: float = 0.25,
        seed: int = 0,
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/triage"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.triage_train"]
            + ["--data", data_uri, "--out", out]
            + ["--target-recall", str(target_recall), "--min-precision", str(min_precision)]
            + ["--seed", str(seed)],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "triage_train.json").read_text())
        for name, value in result["metrics"].items():
            metrics.log_metric(name, float(value))
        metrics.log_metric("p0_threshold", float(result["p0_threshold"]))
        return str(result["version"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def triage_evaluate(
        output_root: str,
        version: str,
        gate: dsl.Output[dsl.Metrics],
        production_summary: str = "data/golden/triage_production.json",
        min_p0_recall: float = 0.85,
        max_ece: float = 0.12,
        max_macro_f1_drop: float = 0.02,
        max_brier_increase: float = 0.01,
        max_p0_recall_drop: float = 0.03,
        force: bool = False,
        champion: str = "registry",
        tenant: str = "solo",
        environment: str = "northwind",
        source_uri: str = "",
        platform_env: str = "",
    ) -> bool:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/triage"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.triage_evaluate"]
            + ["--out", out, "--version", version, "--production-summary", production_summary]
            + ["--min-p0-recall", str(min_p0_recall), "--max-ece", str(max_ece)]
            + ["--max-macro-f1-drop", str(max_macro_f1_drop)]
            + ["--max-brier-increase", str(max_brier_increase)]
            + ["--max-p0-recall-drop", str(max_p0_recall_drop)]
            + ["--force", "true" if force else "false"]
            + ["--champion", champion, "--tenant", tenant, "--environment", environment],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "triage_evaluate.json").read_text())
        for name, value in result["metrics"].items():
            gate.log_metric(name, float(value))
        gate.log_metric("passed", float(result["passed_int"]))
        return bool(result["passed"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def semantic_data_prep(
        data_uri: str,
        output_root: str,
        profile: dsl.Output[dsl.Metrics],
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/semantic"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.semantic_data_prep"]
            + ["--data", data_uri, "--out", out],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "semantic_data_prep.json").read_text())
        profile.log_metric("rows", float(result["n"]))
        profile.log_metric("tags_in_training", float(result["tags_in_training"]))
        return str(result["data_sha256_12"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def semantic_train(
        data_uri: str,
        output_root: str,
        metrics: dsl.Output[dsl.Metrics],
        epochs: int = 2,
        subset: int = 2000,
        lr: float = 1e-3,
        batch_size: int = 16,
        seed: int = 0,
        base: str = "",
        max_length: int = 0,
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/semantic"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        cmd = _launch + ["nw.pipelines.steps.semantic_train"]
        cmd += ["--data", data_uri, "--out", out, "--epochs", str(epochs)]
        cmd += ["--subset", str(subset), "--lr", str(lr), "--batch-size", str(batch_size)]
        cmd += ["--seed", str(seed)]
        if base:
            cmd += ["--base", base]
        if max_length:
            cmd += ["--max-length", str(max_length)]
        subprocess.run(cmd, check=True)
        result = json.loads(pathlib.Path(out, "steps", "semantic_train.json").read_text())
        for name, value in result["metrics"].items():
            metrics.log_metric(name, float(value))
        metrics.log_metric("seconds", float(result["seconds"]))
        return str(result["version"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def semantic_export(
        data_uri: str,
        output_root: str,
        version: str,
        parity_n: int = 20,
        source_uri: str = "",
        platform_env: str = "",
    ) -> float:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/semantic"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.semantic_export"]
            + ["--out", out, "--version", version, "--data", data_uri, "--n", str(parity_n)],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "semantic_export.json").read_text())
        return float(result["max_abs_diff_fp32"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def semantic_benchmark(
        data_uri: str,
        output_root: str,
        version: str,
        table: dsl.Output[dsl.Metrics],
        triage_artifact: str = "",
        latency_n: int = 100,
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/semantic"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        cmd = _launch + ["nw.pipelines.steps.semantic_benchmark"]
        cmd += ["--out", out, "--version", version, "--data", data_uri]
        cmd += ["--latency-n", str(latency_n)]
        if triage_artifact:
            cmd += ["--triage", triage_artifact]
        subprocess.run(cmd, check=True)
        result = json.loads(pathlib.Path(out, "steps", "semantic_benchmark.json").read_text())
        for name, value in result["metrics"].items():
            if value is not None:
                table.log_metric(name, float(value))
        return str(result["triage"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def semantic_gate(
        output_root: str,
        version: str,
        gate: dsl.Output[dsl.Metrics],
        production_summary: str = "data/golden/semantic_production.json",
        min_p0_recall: float = 0.70,
        min_tag_micro_f1: float = 0.50,
        max_int8_p95_ms: float = 100.0,
        max_tag_micro_f1_drop: float = 0.02,
        max_priority_macro_f1_drop: float = 0.02,
        force: bool = False,
        champion: str = "registry",
        tenant: str = "solo",
        environment: str = "northwind",
        source_uri: str = "",
        platform_env: str = "",
    ) -> bool:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/semantic"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.semantic_gate"]
            + ["--out", out, "--version", version, "--production-summary", production_summary]
            + ["--min-p0-recall", str(min_p0_recall)]
            + ["--min-tag-micro-f1", str(min_tag_micro_f1)]
            + ["--max-int8-p95-ms", str(max_int8_p95_ms)]
            + ["--max-tag-micro-f1-drop", str(max_tag_micro_f1_drop)]
            + ["--max-priority-macro-f1-drop", str(max_priority_macro_f1_drop)]
            + ["--force", "true" if force else "false"]
            + ["--champion", champion, "--tenant", tenant, "--environment", environment],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "semantic_gate.json").read_text())
        for name, value in result["metrics"].items():
            gate.log_metric(name, float(value))
        gate.log_metric("passed", float(result["passed_int"]))
        return bool(result["passed"])

    @dsl.component(base_image=image, install_kfp_package=False)
    def register(
        pipeline: str,
        output_root: str,
        version: str,
        passed: bool,
        tenant: str = "solo",
        environment: str = "northwind",
        enabled: bool = True,
        trigger: str = "manual",
        source_uri: str = "",
        platform_env: str = "",
    ) -> str:
        import json
        import pathlib
        import subprocess
        import sys

        out = f"{output_root.replace('gs://', '/gcs/', 1)}/{pipeline}"
        _launch = [sys.executable, "-m", "nw.pipelines.source", "run"]
        if source_uri:
            _launch += ["--source", source_uri]
        if platform_env:
            _launch += ["--env", platform_env]
        _launch += ["--"]
        gate_step = {"triage": "triage_evaluate", "semantic": "semantic_gate"}[pipeline]
        decision = json.loads(pathlib.Path(out, "steps", f"{gate_step}.json").read_text())
        if not passed:
            raise SystemExit(f"gate failed for {version}: {decision['reason']}")
        if not enabled:
            return f"not registered: registration disabled for this run ({version})"
        subprocess.run(
            _launch
            + ["nw.pipelines.steps.register"]
            + ["--pipeline", pipeline, "--out", out, "--version", version]
            + ["--tenant", tenant, "--environment", environment, "--trigger", trigger],
            check=True,
        )
        result = json.loads(pathlib.Path(out, "steps", "register.json").read_text())
        registered = result.get("registered") or {}
        return f"{result['tenant']}-{pipeline} version {registered.get('version')} from {version}"

    return {
        "triage_data_check": triage_data_check,
        "triage_train": triage_train,
        "triage_evaluate": triage_evaluate,
        "semantic_data_prep": semantic_data_prep,
        "semantic_train": semantic_train,
        "semantic_export": semantic_export,
        "semantic_benchmark": semantic_benchmark,
        "semantic_gate": semantic_gate,
        "register": register,
    }
