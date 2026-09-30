"""Every model id the configuration can resolve to has a price. The meter falls back to the
Workhorse rate for an unknown id so a cost is always recorded, but a fallback in production
is a wrong number on the cost dashboard: CI fails on it instead."""

import pytest

from nw.config import DEFAULT_MODELS, EU_MODELS, FAKE_EU_MODELS, FAKE_MODELS
from nw.llm.prices import PRICES, price_for

pytestmark = pytest.mark.session01


def configured_ids() -> set[str]:
    ids: set[str] = set(FAKE_MODELS.values()) | set(FAKE_EU_MODELS.values())
    for roles in DEFAULT_MODELS.values():
        ids |= set(roles.values())
    for roles in EU_MODELS.values():
        ids |= {m for m in roles.values() if m}
    return ids


def test_every_configured_model_id_is_priced():
    unknown = sorted(m for m in configured_ids() if price_for(m)[1])
    assert unknown == [], f"add these to nw/llm/prices.py before shipping: {unknown}"


def test_sonnet_is_priced_at_the_adr_rate():
    for model in ("claude-sonnet-5", "anthropic.claude-sonnet-5"):
        p = PRICES[model]
        assert (p.input_per_mtok, p.output_per_mtok) == (3.00, 15.00), model
