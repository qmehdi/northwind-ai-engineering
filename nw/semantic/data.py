"""Ticket dataset for the encoder: multi-hot tags plus a priority index.

Nothing here touches a GPU. The tokenizer is passed in so tests can use a tiny
one and the input pipeline can be measured on its own, which is where the time
usually goes when people blame the accelerator.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from torch.utils.data import DataLoader, Dataset

from nw.triage.features import PRIORITIES, ticket_text

# The fixed tag vocabulary. Order matters: it is the column order of the multi-hot
# target and of the model's tag head, and it is saved with every artifact.
TAGS: list[str] = [
    "Login",
    "SSO",
    "MFA",
    "Permissions",
    "Provisioning",
    "Invoice",
    "Payment",
    "Refund",
    "Plan",
    "Seats",
    "API",
    "Rate Limit",
    "Webhook",
    "SDK",
    "Deprecation",
    "Sync",
    "Connector",
    "Data Quality",
    "Latency",
    "Performance",
    "Dashboard",
    "Export",
    "Sharing",
    "Chart",
    "Slack",
    "Salesforce",
    "Jira",
    "Okta",
    "Quota",
    "Upload",
    "Retention",
    "Backup",
    "Email",
    "Alerting",
    "Duplicate",
    "Audit Log",
    "Deactivation",
    "Settings",
    "Allowlist",
    "Crash",
    "Offline",
    "Push",
    "Outage",
    "Data Loss",
    "Security",
    "Breach",
    "Compliance",
    "GDPR",
    "Bug",
    "Feature",
    "Documentation",
    "Feedback",
    "How-To",
    "Escalation",
]
TAG_INDEX = {t: i for i, t in enumerate(TAGS)}


def load_rows(path: Path, split: str | None = None) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    if split:
        rows = [r for r in rows if r.get("split") == split]
    return rows


def multi_hot(tags: Sequence[str]) -> torch.Tensor:
    y = torch.zeros(len(TAGS), dtype=torch.float32)
    for t in tags:
        if t in TAG_INDEX:
            y[TAG_INDEX[t]] = 1.0
    return y


@dataclass
class Example:
    text: str
    tags: torch.Tensor
    priority: int


class TicketDataset(Dataset):
    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        self.examples = [
            Example(
                text=ticket_text(r.get("subject", ""), r.get("body", "")),
                tags=multi_hot(r.get("tags", [])),
                priority=PRIORITIES.index(r["priority"]) if r.get("priority") in PRIORITIES else -1,
            )
            for r in rows
        ]

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, i: int) -> Example:
        return self.examples[i]


class Collate:
    """Tokenise a batch with dynamic padding: pad to the longest in the batch, not to
    max_length, which is the single biggest CPU saving in the input pipeline."""

    def __init__(self, tokenizer: Any, max_length: int = 256) -> None:
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __call__(self, batch: Sequence[Example]) -> dict[str, torch.Tensor]:
        enc = self.tokenizer(
            [e.text for e in batch],
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        enc["tags"] = torch.stack([e.tags for e in batch])
        enc["priority"] = torch.tensor([e.priority for e in batch], dtype=torch.long)
        return dict(enc)


def make_loader(
    rows: Sequence[dict[str, Any]],
    tokenizer: Any,
    *,
    batch_size: int = 16,
    shuffle: bool = False,
    max_length: int = 256,
    num_workers: int = 0,
) -> DataLoader:
    return DataLoader(
        TicketDataset(rows),
        batch_size=batch_size,
        shuffle=shuffle,
        collate_fn=Collate(tokenizer, max_length=max_length),
        num_workers=num_workers,
    )


def stratified_subset(
    rows: Sequence[dict[str, Any]], n: int, seed: int = 0
) -> list[dict[str, Any]]:
    """A smaller training set that keeps the priority mix, for laptops."""
    import random

    rng = random.Random(seed)
    by_p: dict[str, list[dict[str, Any]]] = {p: [] for p in PRIORITIES}
    for r in rows:
        if r.get("priority") in by_p:
            by_p[r["priority"]].append(r)
    out: list[dict[str, Any]] = []
    for group in by_p.values():
        k = max(1, round(n * len(group) / max(1, len(rows))))
        rng.shuffle(group)
        out.extend(group[:k])
    rng.shuffle(out)
    return out
