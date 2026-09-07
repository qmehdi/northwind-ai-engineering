"""USD per million tokens, by model ID.

These are list prices for the first-party API at the time of writing. Bedrock
and Vertex bill at their own rates: replace this table from the provider's
pricing page before each delivery, and treat every number as an estimate until
the cloud bill confirms it. Unknown models fall back to the workhorse rate so a
cost is always recorded, and the meter logs the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


_SONNET = Price(2.00, 10.00, 0.20, 2.50)
_OPUS = Price(5.00, 25.00, 0.50, 6.25)
_HAIKU = Price(1.00, 5.00, 0.10, 1.25)

PRICES: dict[str, Price] = {
    # first-party and Vertex IDs
    "claude-sonnet-5": _SONNET,
    "claude-opus-5": _OPUS,
    "claude-haiku-4-5": _HAIKU,
    "claude-haiku-4-5@20251001": _HAIKU,
    # Bedrock IDs
    "anthropic.claude-sonnet-5": _SONNET,
    "anthropic.claude-opus-5": _OPUS,
    "anthropic.claude-haiku-4-5": _HAIKU,
    # fake provider used by tests: priced like the real roles so tests exercise the meter
    "fake-workhorse": _SONNET,
    "fake-judge": _OPUS,
    "fake-economy": _HAIKU,
}

FALLBACK = _SONNET


def price_for(model: str) -> tuple[Price, bool]:
    """Return the price and whether it was a fallback."""
    if model in PRICES:
        return PRICES[model], False
    return FALLBACK, True
