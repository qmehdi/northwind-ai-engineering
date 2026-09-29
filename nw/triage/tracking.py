"""Run tracking and the model registry for Project 1.

Every training run appends one line to `artifacts/triage/runs.jsonl`: version, parameters,
data hash, git SHA and the test metrics. That file is the experiment log the course can
always read, with no server. When MLflow is installed (the `mlops` extra) the same run is
also logged to an MLflow experiment and registered as a version of the registered model
`registered_model_name()` with the alias `candidate`; promotion moves the alias `production`.
The name is the platform's: `<environment>-<tenant>-triage` (`northwind-alice-triage`) when
`NW_TENANT` is set, so a laptop run and a pipeline run land on one model, and `northwind-triage`
when it is not.

    uv run python -m nw.triage.tracking            # the runs table
    make mlflow-ui                                 # the MLflow UI on :5000 over the same store
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from nw.logging import get_logger

log = get_logger("nw.triage.tracking")
MODEL_NAME = "northwind-triage"  # the registered model when no tenant is set
RUNS = Path("artifacts/triage/runs.jsonl")


def mlflow_uri() -> str:
    return os.environ.get("NW_MLFLOW_URI", "sqlite:///artifacts/mlflow.db")


def registered_model_name(project: str = "triage") -> str:
    """`<environment>-<tenant>-<project>` when `NW_TENANT` is set (the name every platform
    registers under, `Tenant.resource`), `northwind-<project>` when it is not."""
    from nw.config import Settings
    from nw.platform.base import tenant_from_env

    cfg = Settings()
    if not cfg.tenant:
        return f"northwind-{project}"
    return tenant_from_env(cfg).resource(project)


def record_run(out: Path, metadata: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Append the run to runs.jsonl and, when MLflow is present, log and register it."""
    params = {
        k: metadata.get(k)
        for k in ("class_weight", "calibrated", "target_p0_recall", "min_p0_precision", "seed")
    }
    t = report["test"]
    metrics = {
        "test_macro_f1": t["macro_f1"],
        "test_p0_recall": t["p0_recall"],
        "test_p0_precision": t["p0_precision"],
        "test_ece": t["ece"],
        "test_brier_p0": t["brier_p0"],
        "p0_threshold": report["p0_threshold"],
    }
    run = {
        "version": metadata["version"],
        "trained_at": metadata["trained_at"],
        "data_sha256_12": metadata["data_sha256_12"],
        "git_sha": metadata["git_sha"],
        "params": params,
        "metrics": metrics,
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
    mlflow.set_experiment("triage")
    with mlflow.start_run(run_name=run["version"]) as active:
        mlflow.log_params(
            {**run["params"], "data_sha256_12": run["data_sha256_12"], "git_sha": run["git_sha"]}
        )
        mlflow.log_metrics(run["metrics"])
        mlflow.log_artifacts(str(artifact_dir), artifact_path="model")
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
        "| version | weights | calibrated | threshold | macro-F1 | P0 recall | P0 precision | ECE |"
    )
    lines = [head, "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for r in runs:
        p, m = r["params"], r["metrics"]
        lines.append(
            f"| {r['version']} | {p.get('class_weight') or 'none'} | {p.get('calibrated')} | "
            f"{m['p0_threshold']:.2f} | {m['test_macro_f1']:.3f} | {m['test_p0_recall']:.3f} | "
            f"{m['test_p0_precision']:.3f} | {m['test_ece']:.3f} |"
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
