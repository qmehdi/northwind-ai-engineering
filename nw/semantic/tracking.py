"""Run tracking and the model registry for Project 2.

Every training run appends one line to `artifacts/semantic/runs.jsonl`: version, the
parameters that matter (subset, epochs, learning rate, LoRA rank, alpha, dropout, max
length, seed), the validation metrics per epoch, the test metrics of the best epoch, the
seconds it took and the device. That file is the experiment log the course can always
read, with no server. When MLflow is installed (the `mlops` extra) the same run is logged
to the experiment `semantic` and registered as a version of `registered_model_name()`
(`<environment>-<tenant>-semantic` when `NW_TENANT` is set, `northwind-semantic` when not) with
the alias `candidate`; the promotion gate moves the alias `production`.

    uv run python -m nw.semantic.tracking            # the runs table
    make mlflow-ui                                   # the MLflow UI over the same store
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from nw.logging import get_logger
from nw.triage.tracking import mlflow_uri
from nw.triage.tracking import registered_model_name as _registered_model_name

log = get_logger("nw.semantic.tracking")
MODEL_NAME = "northwind-semantic"  # the registered model when no tenant is set
RUNS = Path("artifacts/semantic/runs.jsonl")
PARAMS = ("subset", "epochs", "lr", "r", "alpha", "dropout", "max_length", "seed")
# What goes to the registry: the small files. Checkpoints and graphs stay on disk.
LOGGED_FILES = (
    "metadata.json",
    "data_profile.json",
    "best_metrics.json",
    "tag_thresholds.npy",
    "MODEL_CARD.md",
)


def registered_model_name() -> str:
    return _registered_model_name("semantic")


def run_params(metadata: dict[str, Any]) -> dict[str, Any]:
    lora = metadata.get("lora", {})
    return {
        "subset": metadata.get("subset"),
        "epochs": metadata.get("epochs"),
        "lr": metadata.get("lr"),
        "r": lora.get("r"),
        "alpha": lora.get("alpha"),
        "dropout": lora.get("dropout"),
        "max_length": metadata.get("max_length"),
        "seed": metadata.get("seed"),
    }


def record_run(out: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Append the run to runs.jsonl and, when MLflow is present, log and register it."""
    t = metadata["metrics"]["test"]
    metrics = {
        "test_tag_micro_f1": t["tag_micro_f1"],
        "test_tag_macro_f1": t["tag_macro_f1"],
        "test_priority_macro_f1": t["priority_macro_f1"],
        "test_p0_recall": t["p0_recall"],
        "seconds": metadata["seconds"],
    }
    run = {
        "version": metadata["version"],
        "trained_at": metadata["trained_at"],
        "data_sha256_12": metadata["data_sha256_12"],
        "git_sha": metadata["git_sha"],
        "device": metadata["device"],
        "params": run_params(metadata),
        "metrics": metrics,
        "history": metadata.get("history", []),
        "artifact": str(out / metadata["version"]),
    }
    runs = out / "runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run) + "\n")
    run["mlflow"] = _log_to_mlflow(out / metadata["version"], run)
    return run


def _log_to_mlflow(artifact_dir: Path, run: dict[str, Any]) -> dict[str, Any] | None:
    try:
        import mlflow
    except ImportError:
        log.info("mlflow not installed; run recorded in runs.jsonl only")
        return None
    mlflow.set_tracking_uri(mlflow_uri())
    mlflow.set_experiment("semantic")
    with mlflow.start_run(run_name=run["version"]) as active:
        mlflow.log_params(
            {
                **run["params"],
                "device": run["device"],
                "data_sha256_12": run["data_sha256_12"],
                "git_sha": run["git_sha"],
            }
        )
        mlflow.log_metrics(run["metrics"])
        for h in run["history"]:
            mlflow.log_metrics(
                {f"val_{k}": v for k, v in h.items() if k != "epoch"}, step=int(h["epoch"])
            )
        for name in LOGGED_FILES:
            if (artifact_dir / name).exists():
                mlflow.log_artifact(str(artifact_dir / name), artifact_path="model")
        client = mlflow.MlflowClient()
        name = registered_model_name()
        try:
            client.create_registered_model(name)
        except Exception:  # noqa: BLE001  already exists
            pass
        version = client.create_model_version(
            name, source=f"{active.info.artifact_uri}/model", run_id=active.info.run_id
        )
        client.set_registered_model_alias(name, "candidate", version.version)
        client.set_model_version_tag(name, version.version, "artifact_version", run["version"])
    return {
        "run_id": active.info.run_id,
        "registered_model": name,
        "registered_version": int(version.version),
    }


def set_production_alias(artifact_version: str) -> bool:
    """Point the registry's `production` alias at the version whose tag matches."""
    try:
        import mlflow
    except ImportError:
        return False
    mlflow.set_tracking_uri(mlflow_uri())
    client = mlflow.MlflowClient()
    name = registered_model_name()
    for mv in client.search_model_versions(f"name='{name}'"):
        if mv.tags.get("artifact_version") == artifact_version:
            client.set_registered_model_alias(name, "production", mv.version)
            return True
    return False


def load_runs(path: Path = RUNS) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def format_runs(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "no runs yet"
    head = (
        "| version | subset | epochs | lr | r | tag micro-F1 | tag macro-F1 "
        "| priority macro-F1 | P0 recall | seconds | device |"
    )
    lines = [head, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r in runs:
        p, m = r["params"], r["metrics"]
        lines.append(
            f"| {r['version']} | {p.get('subset') or 'all'} | {p.get('epochs')} | "
            f"{p.get('lr'):.0e} | {p.get('r')} | {m['test_tag_micro_f1']:.3f} | "
            f"{m['test_tag_macro_f1']:.3f} | {m['test_priority_macro_f1']:.3f} | "
            f"{m['test_p0_recall']:.3f} | {m['seconds']:.0f} | {r.get('device', '?')} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=RUNS)
    args = ap.parse_args()
    print(format_runs(load_runs(args.runs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
