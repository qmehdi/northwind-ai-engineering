"""The pyfunc wrapper the Local track registers and serves (ADR 0011).

`LocalModelRegistry.register` logs an artifact directory as an MLflow pyfunc model whose
loader knows the course's artifact kinds. Serving is `mlflow models serve` in the serving
tier, which imports this module, so it must import cleanly inside the serving image: only
`mlflow` at the top, the course loaders on demand.

Kinds, detected from the directory:
- triage: `metadata.json` plus `model.joblib`, loaded with `nw.triage.model.TriageModel`;
  `predict` takes rows with `subject` and `body` and returns the service's TriageResult fields.
- anything else registers for lineage and download but does not serve: the service that owns
  the artifact serves it (the semantic engine loads its own int8 graph, the policy index its
  own manifest).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mlflow.pyfunc


def detect_kind(path: Path) -> str:
    if (path / "model.joblib").exists() and (path / "metadata.json").exists():
        return "triage"
    if (path / "manifest.json").exists():
        return "policy-index"
    if (path / "model_int8.onnx").exists() or (path / "best_metrics.json").exists():
        return "semantic"
    return "artifact"


def rows_of(model_input: Any) -> list[dict[str, Any]]:
    """Whatever `mlflow models serve` hands over (dataframe_records, dataframe_split, inputs,
    instances) or a caller passes directly, as a list of row dicts."""
    if hasattr(model_input, "to_dict"):
        return model_input.to_dict(orient="records")
    if isinstance(model_input, dict):
        if all(isinstance(v, list | tuple) for v in model_input.values()):
            keys = list(model_input)
            return [
                dict(zip(keys, vals, strict=True))
                for vals in zip(*model_input.values(), strict=True)
            ]
        return [model_input]
    if isinstance(model_input, list | tuple):
        return [dict(r) for r in model_input]
    raise TypeError(f"unsupported input {type(model_input).__name__}")


class ArtifactModel(mlflow.pyfunc.PythonModel):
    """One pyfunc for every registered artifact directory."""

    def load_context(self, context: Any) -> None:
        self.path = Path(context.artifacts["artifact"])
        self.kind = detect_kind(self.path)
        self.model = None
        if self.kind == "triage":
            from nw.triage.model import TriageModel

            self.model = TriageModel.load(self.path)

    def predict(
        self, context, model_input, params=None
    ):  # no hints: MLflow infers a signature from them
        if self.model is None:
            raise ValueError(
                f"a {self.kind} artifact registers but does not serve here; "
                "the service that owns it serves it"
            )
        return [r.model_dump() for r in self.model.predict(rows_of(model_input))]
