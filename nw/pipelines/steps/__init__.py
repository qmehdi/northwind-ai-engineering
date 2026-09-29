"""The shared step code: what both pipeline SDKs run.

Every step is a function with paths in and paths out, plus a `main()` so an orchestrator can
run it as `python -m nw.pipelines.steps.<name> --out <root> ...`. Steps for Project 1 write
under `<root>` laid out exactly like `artifacts/triage`; Project 2 like `artifacts/semantic`,
so the service, the backtest and the model card read a pipeline's output unchanged.

| Step | Module | Reads | Writes |
| --- | --- | --- | --- |
| data check | `triage_data_check` | tickets | `data_profile.json`, `data_check.json` |
| train | `triage_train` | tickets | `<version>/`, `runs.jsonl` |
| evaluate | `triage_evaluate` | `<version>/`, production summary | `<version>/gate.json` |
| data prep | `semantic_data_prep` | tickets | `data_profile.json`, `data_check.json` |
| train | `semantic_train` | tickets | `<version>/`, `runs.jsonl` |
| export | `semantic_export` | `<version>/` | `<version>/model*.onnx`, `export_report.json` |
| benchmark | `semantic_benchmark` | `<version>/`, a Project 1 model | `<version>/benchmark.json` |
| gate | `semantic_gate` | `<version>/`, production summary | `<version>/gate.json` |
| register | `register` | `<version>/`, the gate decision | `<version>/register.json` |

Each step also writes `steps/<name>.json`, the result of its latest run at the root, which
is what a SageMaker PropertyFile or a Kubeflow component reads without knowing the version.
The registry is touched only by `register`, through the track's platform.
"""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path
from typing import Any

STEP_RESULTS = "steps"
# Files that never leave the training machine: checkpoints and the fp32 graph.
NOT_PACKAGED = ("checkpoint.pt", "best.pt", "model.onnx")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=str) + "\n", encoding="utf-8")
    return path


def write_result(out: Path, step: str, payload: dict[str, Any]) -> Path:
    """The step's result at `<out>/steps/<step>.json`, overwritten on every run."""
    return write_json(Path(out) / STEP_RESULTS / f"{step}.json", payload)


def read_result(out: Path, step: str) -> dict[str, Any]:
    path = Path(out) / STEP_RESULTS / f"{step}.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: the {step} step must run first")
    return read_json(path)


def localize(uri: str | Path, suffix: str = ".jsonl") -> Path:
    """The local path a data URI means inside a step's container.

    Vertex AI Pipelines mounts every bucket under `/gcs/<bucket>/`, so a `gs://` URI is a
    directory away. SageMaker downloads an S3 input into a directory before the step starts,
    so a directory means the one file of `suffix` inside it. Anything else is a path."""
    text = str(uri)
    path = Path("/gcs") / text[len("gs://") :] if text.startswith("gs://") else Path(text)
    if path.is_dir():
        found = sorted(p for p in path.iterdir() if p.suffix == suffix)
        if len(found) != 1:
            raise SystemExit(f"{path}: expected one {suffix} file, found {len(found)}")
        return found[0]
    return path


def truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def add_flag(ap: Any, name: str, help: str) -> None:  # noqa: A002
    """`--force` alone means true; `--force false` and its absence mean false, so a pipeline
    parameter that is a string can drive the flag."""
    ap.add_argument(name, nargs="?", const="true", default="false", help=help)


def package(version_dir: Path, into: Path, name: str = "model.tar.gz") -> Path:
    """`model.tar.gz` of one version directory, the shape a model registry ingests: the
    artifact plus `code/inference.py` for SageMaker's prebuilt containers
    (`nw.serving.sagemaker.package`). Checkpoints and the fp32 graph stay out."""
    try:
        from nw.serving.sagemaker.package import package as sagemaker_package

        return sagemaker_package(Path(version_dir), Path(into), name)
    except FileNotFoundError:
        pass  # not a recognisable artifact: the plain archive below
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    target = into / name
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(Path(version_dir).rglob("*")):
            if path.is_file() and path.name not in NOT_PACKAGED:
                tar.add(path, arcname=str(path.relative_to(version_dir)))
    return target


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "NOT_PACKAGED",
    "STEP_RESULTS",
    "add_flag",
    "env_flag",
    "localize",
    "package",
    "read_json",
    "read_result",
    "truthy",
    "write_json",
    "write_result",
]
