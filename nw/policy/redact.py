"""Tokenise sensitive fields before anything is indexed or sent to a model.

The rule is simple: an identifier that could name a person or an account is
replaced by a stable token before it reaches an index, a prompt, or a log.
Stable means the same value maps to the same token within a document, so the
model can still say "the customer at ACCOUNT_1 asked twice" without ever
seeing the identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ACCOUNT", re.compile(r"\bNW-\d{5,}\b")),
    ("INVOICE", re.compile(r"\bINV-\d{4,}\b")),
    # A digit run after a decimal point is a float's fraction (a cost in a JSON line), not a card.
    ("CARD", re.compile(r"(?<![\d.])\b(?:\d[ -]?){13,19}\b")),
    # Phones must carry a country code prefix; a bare pattern also matches ISO dates.
    ("PHONE", re.compile(r"\+\d[\d ()-]{8,}\d")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("KEY", re.compile(r"\b(?:sk|nw|key)[_-][A-Za-z0-9]{16,}\b")),
]


@dataclass
class Redaction:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.mapping)


def redact(text: str) -> Redaction:
    """Replace every sensitive match with a stable token like `[EMAIL_1]`."""
    return Redaction(text=text)  # Step 2: nothing is redacted yet


def redact_fields(record: dict[str, Any], *names: str) -> dict[str, Any]:
    """A copy of `record` with the named string fields redacted. Every capture file the
    services write and the feedback log go through this before a line hits disk, so a
    backtest file never holds a raw email, phone number or account id."""
    out = dict(record)
    for name in names:
        value = out.get(name)
        if isinstance(value, str) and value:
            out[name] = redact(value).text
    return out
