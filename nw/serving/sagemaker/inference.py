"""The SageMaker inference handler for both course models, one file for both containers.

The SageMaker inference toolkit loads `code/inference.py` from the model artifact and calls four
functions (the prebuilt scikit-learn and PyTorch containers share the contract; PyTorch passes an
optional `context`, accepted here and ignored):

- `model_fn(model_dir)`: load the artifact SageMaker unpacked into `/opt/ml/model`
- `input_fn(request_body, content_type)`: the request to a list of tickets
- `predict_fn(instances, model)`: one prediction per ticket
- `output_fn(prediction, accept)`: the response body

The artifact says which model it is: `model.joblib` is Project 1 (the triage `TriageModel`,
whose pickle needs `nw.triage` importable, which `package.py` vendors beside this file);
`model.int8.onnx` or `model.onnx` is Project 2 (the ONNX graph with the tokenizer and the tag
thresholds, run here with onnxruntime and no torch). Every prediction carries `text_length`,
the feature the Model Monitor baseline describes, so the captured output can be monitored
without a preprocessor when the schedule is pointed at the output.

Request shapes accepted (`application/json`): `{"instances": [{"subject", "body"}, ...]}`,
a bare list, or one ticket object; `application/jsonlines` with one ticket per line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

PRIORITIES = ["P0", "P1", "P2", "P3"]
JSON = "application/json"
JSONLINES = "application/jsonlines"


def ticket_text(subject: str, body: str) -> str:
    """The same string `nw.triage.features.ticket_text` builds: subject twice, then body."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    return f"{subject}\n{subject}\n{body}"


def text_length(ticket: dict[str, Any]) -> int:
    return len(str(ticket.get("subject") or "")) + len(str(ticket.get("body") or ""))


def base_revision(meta: dict[str, Any]) -> str:
    """The Hub commit of the base model: the artifact's own `base_revision`, else the course pin
    (`nw.config.HF_REVISIONS`, importable in a course image, not in a prebuilt container).
    Never `main`: an unpinned download is refused."""
    if meta.get("base_revision"):
        return str(meta["base_revision"])
    try:
        from nw.config import hf_revision
    except ImportError as exc:
        raise ValueError(
            "artifact has no tokenizer/ and no base_revision; refusing an unpinned download"
        ) from exc
    revision = hf_revision(str(meta["base"]))
    if not revision:
        raise ValueError(f"no pinned revision for {meta['base']!r}")
    return revision


class TriagePredictor:
    kind = "triage"

    def __init__(self, model_dir: Path) -> None:
        from nw.triage.model import TriageModel

        # The registry's hash of model.joblib, when the deployer injected it, is checked before
        # the pickle runs; otherwise the hash the training run wrote into metadata.json.
        expected = (os.environ.get("NW_MODEL_SHA256") or "").strip().lower() or None
        self.model = TriageModel.load(model_dir, expected_sha256=expected)
        self.version = self.model.version

    def predict(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [
            {"subject": str(t.get("subject") or ""), "body": str(t.get("body") or "")}
            for t in tickets
        ]
        out = []
        for ticket, result in zip(rows, self.model.predict(rows), strict=True):
            out.append({**result.model_dump(), "text_length": text_length(ticket)})
        return out


class SemanticPredictor:
    kind = "semantic"

    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        meta = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
        self.version = str(meta.get("version", "unknown"))
        self.tags = list(meta["tags"])
        self.max_length = int(meta.get("max_length", 256))
        tok_dir = model_dir / "tokenizer"
        if tok_dir.is_dir():
            self.tokenizer = AutoTokenizer.from_pretrained(str(tok_dir))
        else:  # the base model's tokenizer from the Hub, pinned to a commit
            self.tokenizer = AutoTokenizer.from_pretrained(
                meta["base"], revision=base_revision(meta)
            )
        self.file = "model.int8.onnx" if (model_dir / "model.int8.onnx").exists() else "model.onnx"
        opts = ort.SessionOptions()
        threads = os.environ.get("NW_ONNX_THREADS")
        if threads:
            opts.intra_op_num_threads = int(threads)
        self.session = ort.InferenceSession(
            str(model_dir / self.file), opts, providers=["CPUExecutionProvider"]
        )
        # A plain float array: never let a crafted .npy unpickle an object array.
        self.thresholds = np.load(model_dir / "tag_thresholds.npy", allow_pickle=False)

    def predict(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        texts = [
            ticket_text(str(t.get("subject") or ""), str(t.get("body") or "")) for t in tickets
        ]
        enc = self.tokenizer(
            texts, padding=True, truncation=True, max_length=self.max_length, return_tensors="np"
        )
        tag_logits, prio_logits, _ = self.session.run(
            None,
            {
                "input_ids": enc["input_ids"].astype(np.int64),
                "attention_mask": enc["attention_mask"].astype(np.int64),
            },
        )
        out = []
        for ticket, tl, pl in zip(tickets, tag_logits, prio_logits, strict=True):
            tag_p = 1 / (1 + np.exp(-tl))
            prio_p = np.exp(pl - pl.max())
            prio_p /= prio_p.sum()
            out.append(
                {
                    "tags": [
                        t
                        for t, p, th in zip(self.tags, tag_p, self.thresholds, strict=True)
                        if p >= th
                    ],
                    "tag_scores": {
                        t: round(float(p), 4)
                        for t, p in zip(self.tags, tag_p, strict=True)
                        if p >= 0.05
                    },
                    "priority": PRIORITIES[int(prio_p.argmax())],
                    "priority_scores": {
                        p: round(float(v), 4) for p, v in zip(PRIORITIES, prio_p, strict=True)
                    },
                    "model_version": self.version,
                    "format": self.file,
                    "text_length": text_length(ticket),
                }
            )
        return out


# ----- the toolkit contract -------------------------------------------------------------


def model_fn(model_dir: str, context: Any = None) -> TriagePredictor | SemanticPredictor:
    d = Path(model_dir)
    if (d / "model.joblib").is_file():
        return TriagePredictor(d)
    if (d / "model.int8.onnx").is_file() or (d / "model.onnx").is_file():
        return SemanticPredictor(d)
    raise FileNotFoundError(
        f"{d}: neither model.joblib (triage) nor model.int8.onnx/model.onnx (semantic) found"
    )


def input_fn(
    request_body: Any, content_type: str = JSON, context: Any = None
) -> list[dict[str, Any]]:
    text = (
        request_body.decode("utf-8")
        if isinstance(request_body, bytes | bytearray)
        else str(request_body)
    )
    ctype = (content_type or JSON).split(";")[0].strip().lower()
    if ctype == JSONLINES:
        parsed: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif ctype == JSON:
        parsed = json.loads(text)
    else:
        raise ValueError(f"unsupported content type {content_type!r}; send {JSON} or {JSONLINES}")
    if isinstance(parsed, dict) and "instances" in parsed:
        parsed = parsed["instances"]
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not all(isinstance(t, dict) for t in parsed):
        raise ValueError("expected a ticket object, a list of them, or {'instances': [...]}")
    for t in parsed:
        if not str(t.get("body") or "").strip():
            raise ValueError("every ticket needs a non-empty 'body'")
    return parsed


def predict_fn(
    instances: list[dict[str, Any]], model: TriagePredictor | SemanticPredictor, context: Any = None
) -> list[dict[str, Any]]:
    return model.predict(instances)


def output_fn(prediction: list[dict[str, Any]], accept: str = JSON, context: Any = None) -> str:
    acc = (accept or JSON).split(";")[0].strip().lower()
    if acc == JSONLINES:
        return "\n".join(json.dumps(p) for p in prediction) + "\n"
    return json.dumps({"predictions": prediction})
