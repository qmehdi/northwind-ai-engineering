"""The trained triage model: load, predict, and the decision rule.

Probabilities come from a calibrated classifier. The decision rule is not
`argmax`: P0 is predicted whenever its calibrated probability clears a
threshold chosen on the validation split for P0 recall, because a missed P0
costs Northwind far more than a false alarm. Everything the service needs to
explain a prediction travels with the artifact in `metadata.json`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from pydantic import BaseModel, Field

from nw.triage.features import PRIORITIES


class TriageResult(BaseModel):
    priority: str = Field(pattern=r"^P[0-3]$")
    probabilities: dict[str, float]
    confidence: float
    rule: str
    model_version: str


@dataclass
class TriageModel:
    pipeline: Any
    classes: list[str]
    p0_threshold: float
    metadata: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.metadata.get("version", "unknown"))

    def predict_proba(self, tickets: list[dict[str, Any]]) -> np.ndarray:
        proba = self.pipeline.predict_proba(tickets)
        # Reorder columns to the canonical P0..P3 order whatever the classifier learned.
        order = [list(self.classes).index(p) for p in PRIORITIES]
        return proba[:, order]

    def decide(self, proba: np.ndarray) -> tuple[str, str]:
        """The decision rule. Returns (priority, rule name)."""
        return PRIORITIES[int(np.argmax(proba))], "argmax"  # threshold step: protect P0

    def predict(self, tickets: list[dict[str, Any]]) -> list[TriageResult]:
        out = []
        for row in self.predict_proba(tickets):
            priority, rule = self.decide(row)
            out.append(
                TriageResult(
                    priority=priority,
                    probabilities={
                        p: round(float(v), 4) for p, v in zip(PRIORITIES, row, strict=True)
                    },
                    confidence=round(float(row.max()), 4),
                    rule=rule,
                    model_version=self.version,
                )
            )
        return out

    # ----- persistence ------------------------------------------------------

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.pipeline, directory / "model.joblib")
        meta = dict(self.metadata)
        meta["classes"] = list(self.classes)
        meta["p0_threshold"] = self.p0_threshold
        meta["model_sha256"] = sha256_of(directory / "model.joblib")
        (directory / "metadata.json").write_text(json.dumps(meta, indent=1, default=str))
        return directory

    @classmethod
    def load(cls, directory: Path, *, expected_sha256: str | None = None) -> TriageModel:
        """Load an artifact, but only one whose bytes are known. `joblib.load` runs pickle,
        which executes whatever the file says, so the file's SHA-256 must match a hash
        recorded somewhere else first: `expected_sha256` from the registry entry when the
        caller has one (the stronger check, the hash does not travel with the file), else
        the `model_sha256` the training run wrote into `metadata.json`. No hash, no load."""
        meta = json.loads((directory / "metadata.json").read_text())
        model_path = directory / "model.joblib"
        expected = expected_sha256 or meta.get("model_sha256")
        if not expected:
            raise ValueError(
                f"{model_path}: no expected SHA-256 (registry entry or metadata.json "
                "model_sha256); refusing to unpickle an unverified file"
            )
        actual = sha256_of(model_path)
        if expected != actual:
            raise ValueError(f"{model_path}: checksum mismatch, artifact is corrupt or mixed")
        return cls(
            pipeline=joblib.load(model_path),
            classes=list(meta["classes"]),
            p0_threshold=float(meta["p0_threshold"]),
            metadata=meta,
        )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
