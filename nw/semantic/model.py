"""The encoder with two heads, and LoRA on its attention projections.

Memory arithmetic, so the numbers on the slide are real: DistilBERT has about
66M parameters. Full fine-tuning in fp32 with Adam needs weights, gradients,
and two optimiser moments: 4 bytes times 4 per parameter, about 1 GB before
activations. LoRA with rank 8 on the query and value projections trains under
one percent of that, so the optimiser state fits anywhere and a laptop is enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch
from torch import nn
from transformers import AutoModel, AutoTokenizer, PretrainedConfig

from nw.semantic.data import TAGS
from nw.triage.features import PRIORITIES

DEFAULT_BASE = "distilbert-base-uncased"


@dataclass
class ModelSpec:
    base: str = DEFAULT_BASE
    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = ("q_lin", "v_lin")  # DistilBERT attention projections
    max_length: int = 256


class TicketEncoder(nn.Module):
    """Shared encoder, mean pooled, two linear heads. The pooled vector doubles as the
    embedding the similarity index stores."""

    def __init__(self, encoder: nn.Module, hidden: int) -> None:
        super().__init__()
        self.encoder = encoder
        self.tag_head = nn.Linear(hidden, len(TAGS))
        self.priority_head = nn.Linear(hidden, len(PRIORITIES))

    def pool(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        hidden = out.last_hidden_state
        mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
        return (hidden * mask).sum(1) / mask.sum(1).clamp(min=1.0)

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        pooled = self.pool(input_ids, attention_mask)
        return self.tag_head(pooled), self.priority_head(pooled)


def build_encoder(
    spec: ModelSpec | None = None, *, config: PretrainedConfig | None = None
) -> tuple[TicketEncoder, Any]:
    """Pretrained weights by name, or a random tiny model from `config` for tests."""
    spec = spec or ModelSpec()
    if config is not None:
        base = AutoModel.from_config(config)
        tokenizer = AutoTokenizer.from_pretrained(spec.base) if spec.base else None
    else:
        base = AutoModel.from_pretrained(spec.base)
        tokenizer = AutoTokenizer.from_pretrained(spec.base)
    hidden = base.config.hidden_size if hasattr(base.config, "hidden_size") else base.config.dim
    return TicketEncoder(base, hidden), tokenizer


def apply_lora(model: TicketEncoder, spec: ModelSpec) -> TicketEncoder:
    """Freeze the encoder and inject LoRA adapters into the attention projections.
    The heads stay trainable. Returns the same module, modified in place."""
    raise NotImplementedError("Freeze the encoder, inject LoRA")


def merge_lora(model: TicketEncoder) -> TicketEncoder:
    """Fold the adapters into the base weights so export sees a plain encoder."""
    if hasattr(model.encoder, "merge_and_unload"):
        model.encoder = model.encoder.merge_and_unload()
    return model


def count_parameters(model: nn.Module) -> tuple[int, int]:
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return total, trainable


def tiny_config() -> PretrainedConfig:
    """A DistilBERT-shaped config small enough to run tests on CPU in seconds."""
    from transformers import DistilBertConfig

    return DistilBertConfig(
        n_layers=1, dim=32, hidden_dim=64, n_heads=2, vocab_size=30522, max_position_embeddings=512
    )


def pick_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")
