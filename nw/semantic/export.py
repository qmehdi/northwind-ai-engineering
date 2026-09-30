"""Export the fine-tuned encoder to ONNX, quantise it, and prove parity.

    uv run python -m nw.semantic.export                         # the newest candidate
    uv run python -m nw.semantic.export --artifact artifacts/semantic/<version>

Parity is checked against the PyTorch model on held-out tickets: fp32 ONNX
must match within 1e-3 on logits here, and within 1e-4 at the promotion gate; the
int8 model is allowed a looser tolerance and is judged by the benchmark on task
metrics, not by logit equality. The graphs, the tokenizer and `export_report.json`
are written into the version directory, where the gate reads them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch

from nw.semantic.artifacts import resolve
from nw.semantic.data import load_rows
from nw.semantic.model import ModelSpec, TicketEncoder, build_encoder, merge_lora
from nw.semantic.train import load_checkpoint
from nw.triage.features import ticket_text


class ExportWrapper(torch.nn.Module):
    """ONNX wants one forward with plain tensor outputs, in a fixed order."""

    def __init__(self, model: TicketEncoder) -> None:
        super().__init__()
        self.model = model

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        pooled = self.model.pool(input_ids, attention_mask)
        return self.model.tag_head(pooled), self.model.priority_head(pooled), pooled


def load_finetuned(
    artifact: Path, *, config: Any = None, tokenizer: Any = None
) -> tuple[TicketEncoder, Any, dict[str, Any]]:
    artifact = resolve(artifact)
    meta = json.loads((artifact / "metadata.json").read_text())
    spec = ModelSpec(
        base=meta["base"],
        lora_r=meta["lora"]["r"],
        lora_alpha=meta["lora"]["alpha"],
        target_modules=tuple(meta["lora"]["targets"]),
        max_length=meta["max_length"],
    )
    from nw.semantic.model import apply_lora

    model, tok = build_encoder(spec, config=config)
    model = apply_lora(model, spec)
    load_checkpoint(artifact / "best.pt", model)
    model = merge_lora(model).eval()
    return model, tokenizer or tok, meta


def export_onnx(model: TicketEncoder, tokenizer: Any, path: Path, max_length: int = 256) -> Path:
    raise NotImplementedError("Export to ONNX: torch.onnx.export with dynamic axes")


def quantize(src: Path, dst: Path) -> Path:
    """Dynamic int8 quantisation of the linear layers: weights stored as int8, activations
    quantised on the fly. No calibration set needed, which is why it is the first thing
    to try on CPU."""
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8)
    return dst


class OnnxEncoder:
    """Run the exported graph. Same outputs as the PyTorch model, no torch at runtime."""

    def __init__(
        self, path: Path, tokenizer: Any, max_length: int = 256, threads: int | None = None
    ) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.tokenizer = tokenizer
        self.max_length = max_length

    def run(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        enc = self.tokenizer(
            texts, padding=True, truncation=True, max_length=self.max_length, return_tensors="np"
        )
        outputs = self.session.run(
            None,
            {
                "input_ids": enc["input_ids"].astype(np.int64),
                "attention_mask": enc["attention_mask"].astype(np.int64),
            },
        )
        return outputs[0], outputs[1], outputs[2]


def parity(
    model: TicketEncoder,
    onnx_model: OnnxEncoder,
    tokenizer: Any,
    texts: list[str],
    max_length: int = 256,
) -> float:
    """Max absolute difference on tag logits between PyTorch and ONNX for `texts`."""
    model.eval()
    with torch.no_grad():
        enc = tokenizer(
            texts, padding=True, truncation=True, max_length=max_length, return_tensors="pt"
        )
        tl, _ = model(enc["input_ids"], enc["attention_mask"])
    otl, _, _ = onnx_model.run(texts)
    return float(np.abs(tl.numpy() - otl).max())


def run(
    artifact: Path, data: Path, n: int = 20, *, config: Any = None, tokenizer: Any = None
) -> dict[str, Any]:
    """Export fp32 and int8 into the version directory, prove parity on `n` held-out
    tickets, save the tokenizer beside the graphs, write `export_report.json`."""
    artifact = resolve(artifact)
    model, tokenizer, meta = load_finetuned(artifact, config=config, tokenizer=tokenizer)
    fp32 = export_onnx(model, tokenizer, artifact / "model.onnx", meta["max_length"])
    int8 = quantize(fp32, artifact / "model.int8.onnx")
    rows = load_rows(data, "test")[:n]
    texts = [ticket_text(r["subject"], r["body"]) for r in rows]
    d32 = parity(
        model,
        OnnxEncoder(fp32, tokenizer, meta["max_length"]),
        tokenizer,
        texts,
        meta["max_length"],
    )
    d8 = parity(
        model,
        OnnxEncoder(int8, tokenizer, meta["max_length"]),
        tokenizer,
        texts,
        meta["max_length"],
    )
    report = {
        "version": meta["version"],
        "onnx_fp32_bytes": fp32.stat().st_size,
        "onnx_int8_bytes": int8.stat().st_size,
        "size_ratio": round(fp32.stat().st_size / int8.stat().st_size, 2),
        "max_abs_diff_fp32": d32,
        "max_abs_diff_int8": d8,
        "parity_fp32_ok": d32 < 1e-3,
        "parity_n": len(texts),
    }
    (artifact / "export_report.json").write_text(json.dumps(report, indent=1))
    # Keep the tokenizer with the artifact so the ONNX runtime image needs no hub access.
    tokenizer.save_pretrained(artifact / "tokenizer")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--artifact",
        type=Path,
        default=Path("artifacts/semantic"),
        help="a version directory, or the root, which means the newest candidate",
    )
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    artifact = resolve(args.artifact)
    print(f"export {artifact}")
    report = run(artifact, args.data, args.n)
    print(json.dumps(report, indent=1))
    return 0 if report["parity_fp32_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
