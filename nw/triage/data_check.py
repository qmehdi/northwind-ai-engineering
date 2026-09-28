"""Validate the training data before a single model trains, and profile it for drift.

    uv run python -m nw.triage.data_check                 # exit 1 on any failed expectation
    uv run python -m nw.triage.data_check --data path.jsonl --out artifacts/triage/data_profile.json

Two outputs. Findings: every expectation the data breaks (schema, label set, split
sizes, P0 share, duplicates, empty bodies). A profile: per-split counts, the label
distribution, text-length quantiles and the language mix. The profile ships inside the
model artifact and is the baseline the service measures drift against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from nw.triage.features import PRIORITIES

SPLITS = ("train", "val", "test")
LENGTH_QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)


class TicketRow(BaseModel):
    """The contract every training row must meet. Extra fields are allowed."""

    ticket_id: str = Field(pattern=r"^T-\d{6}$")
    account_id: str = Field(pattern=r"^NW-\d{5}$")
    subject: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    priority: str = Field(pattern=r"^P[0-3]$")
    split: str = Field(pattern=r"^(train|val|test)$")
    language: str = Field(default="en", min_length=2, max_length=5)


@dataclass
class Expectations:
    p0_share_min: float = 0.02
    p0_share_max: float = 0.08
    min_split_share: float = 0.08  # val and test each
    max_duplicate_share: float = 0.005
    min_rows: int = 500


@dataclass
class Finding:
    check: str
    detail: str
    blocking: bool = True


@dataclass
class DataProfile:
    n: int
    data_sha256_12: str
    splits: dict[str, int]
    priority_share: dict[str, float]
    language_share: dict[str, float]
    text_length_quantiles: dict[str, float]  # over subject + body, characters
    text_length_bins: list[float]  # edges for drift histograms
    text_length_hist: list[float]  # share per bin, training split
    duplicates: int
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f["blocking"] for f in self.findings)


def text_length(row: dict[str, Any]) -> int:
    return len(row.get("subject", "")) + len(row.get("body", ""))


def validate(rows: list[dict[str, Any]], exp: Expectations | None = None) -> list[Finding]:
    """Every expectation the data breaks. Empty list means train."""
    exp = exp or Expectations()
    out: list[Finding] = []
    if len(rows) < exp.min_rows:
        out.append(Finding("row_count", f"{len(rows)} rows, need at least {exp.min_rows}"))
    bad = 0
    for i, r in enumerate(rows):
        try:
            TicketRow.model_validate(r)
        except ValidationError as e:
            bad += 1
            if bad <= 3:
                first = e.errors()[0]
                out.append(
                    Finding(
                        "schema",
                        f"row {i} ({r.get('ticket_id', '?')}): {first['loc']}: {first['msg']}",
                    )
                )
    if bad > 3:
        out.append(Finding("schema", f"{bad} rows fail the schema in total"))
    if not rows:
        return out
    n = len(rows)
    splits = Counter(r.get("split") for r in rows)
    for s in ("val", "test"):
        share = splits.get(s, 0) / n
        if share < exp.min_split_share:
            out.append(
                Finding("split_size", f"{s} is {share:.1%} of rows, need {exp.min_split_share:.0%}")
            )
    prio = Counter(r.get("priority") for r in rows)
    p0 = prio.get("P0", 0) / n
    if not exp.p0_share_min <= p0 <= exp.p0_share_max:
        out.append(
            Finding(
                "p0_share",
                f"P0 is {p0:.1%} of rows; expected {exp.p0_share_min:.0%} "
                f"to {exp.p0_share_max:.0%}",
            )
        )
    missing_labels = [p for p in PRIORITIES if prio.get(p, 0) == 0]
    if missing_labels:
        out.append(Finding("label_set", f"no rows for {missing_labels}"))
    for s in SPLITS:
        if not any(r.get("split") == s and r.get("priority") == "P0" for r in rows):
            out.append(Finding("p0_in_split", f"no P0 rows in the {s} split"))
    keys = Counter((r.get("subject", ""), r.get("body", "")) for r in rows)
    dups = sum(c - 1 for c in keys.values() if c > 1)
    if dups / n > exp.max_duplicate_share:
        out.append(
            Finding("duplicates", f"{dups} duplicate subject and body pairs ({dups / n:.2%})")
        )
    leak = _split_leak(rows)
    if leak:
        out.append(Finding("split_leak", f"{leak} identical tickets appear in more than one split"))
    return out


def _split_leak(rows: list[dict[str, Any]]) -> int:
    seen: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        seen.setdefault((r.get("subject", ""), r.get("body", "")), set()).add(r.get("split", ""))
    return sum(1 for s in seen.values() if len(s) > 1)


def profile(
    rows: list[dict[str, Any]], data_sha: str = "", findings: list[Finding] | None = None
) -> DataProfile:
    n = len(rows)
    lengths = np.asarray([text_length(r) for r in rows], dtype=float)
    train_lengths = np.asarray(
        [text_length(r) for r in rows if r.get("split", "train") == "train"], dtype=float
    )
    if train_lengths.size == 0:
        train_lengths = lengths
    edges = (
        [0.0] + [float(np.quantile(train_lengths, q)) for q in LENGTH_QUANTILES] + [float("inf")]
    )
    hist = np.histogram(train_lengths, bins=edges)[0] / max(train_lengths.size, 1)
    prio = Counter(r.get("priority") for r in rows)
    lang = Counter(r.get("language", "en") for r in rows)
    keys = Counter((r.get("subject", ""), r.get("body", "")) for r in rows)
    return DataProfile(
        n=n,
        data_sha256_12=data_sha,
        splits={s: sum(1 for r in rows if r.get("split") == s) for s in SPLITS},
        priority_share={p: prio.get(p, 0) / max(n, 1) for p in PRIORITIES},
        language_share={k: v / max(n, 1) for k, v in sorted(lang.items())},
        text_length_quantiles={str(q): float(np.quantile(lengths, q)) for q in LENGTH_QUANTILES},
        text_length_bins=edges,
        text_length_hist=[float(x) for x in hist],
        duplicates=sum(c - 1 for c in keys.values() if c > 1),
        findings=[asdict(f) for f in (findings or [])],
    )


def check_file(path: Path, exp: Expectations | None = None) -> DataProfile:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    sha = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return profile(rows, sha, validate(rows, exp))


def format_profile(p: DataProfile) -> str:
    lines = [
        f"rows {p.n}  data {p.data_sha256_12}  splits "
        + ", ".join(f"{k} {v}" for k, v in p.splits.items()),
        "priority " + ", ".join(f"{k} {v:.1%}" for k, v in p.priority_share.items()),
        "language " + ", ".join(f"{k} {v:.1%}" for k, v in p.language_share.items()),
        "text length p10 to p90 " + " ".join(f"{v:.0f}" for v in p.text_length_quantiles.values()),
        f"duplicates {p.duplicates}",
    ]
    for f in p.findings:
        lines.append(f"{'FAIL' if f['blocking'] else 'WARN'} {f['check']}: {f['detail']}")
    lines.append("DATA OK" if p.ok else "DATA NOT OK")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/triage/data_profile.json"))
    args = ap.parse_args()
    p = check_file(args.data)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(p), indent=1))
    print(format_profile(p))
    return 0 if p.ok else 1


if __name__ == "__main__":
    sys.exit(main())
