"""USD per million tokens, by model ID.

First-party list prices for Vertex IDs and the fake provider; Bedrock global
endpoint prices for the `anthropic.` IDs (verified 2026-09-08); open-weight and Nova
prices per ADR 0010 (Bedrock and Google pricing pages, fetched 2026-09-29; Nova Micro
and gpt-oss-20b on Google from third-party trackers of the same pages, the vendor
tables being too long for the fetch, so confirm them in the delivery week). Ollama
is priced at zero: the laptop is already paid for. Azure (ADR 0013), eastus2 Global Standard,
fetched 2026-09-29: gpt-oss-120b from the Azure Retail Prices API (meters `gpt-oss-120B Inp
glbl` and `Outp glbl`); mistral-small-2503 is sold through Azure Marketplace and has no
Retail Prices row, so its rate is the Foundry catalogue price recorded by third-party
trackers (confirm in the portal's Pricing tab); Claude on Foundry bills Anthropic's own
per-model rates in Claude Consumption Units (Learn, claude-models-billing, updated
2026-09-11), so claude-opus-5 already carries the right price. Replace this table from the
provider's pricing page before each delivery, and treat every number as an estimate
until the cloud bill confirms it. Unknown models fall back to the workhorse rate so a
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
# Bedrock global endpoint price for Sonnet 5 from 2026-09-01 (launch pricing ended
# 2026-08-31); in-region endpoints are 10 percent higher.
_SONNET_BEDROCK = Price(3.00, 15.00, 0.30, 3.75)
# Open-weight and Amazon models (ADR 0010). Bedrock in-region on-demand, USD per MTok.
_GPT_OSS_120B_BEDROCK = Price(0.15, 0.60, 0.15, 0.15)
_GPT_OSS_20B_BEDROCK = Price(0.07, 0.20, 0.07, 0.07)
_NOVA_MICRO = Price(0.035, 0.14, 0.00875, 0.035)
# Google's managed API for open models, standard requests.
_GPT_OSS_120B_GOOGLE = Price(0.09, 0.36, 0.09, 0.09)
_GPT_OSS_20B_GOOGLE = Price(0.07, 0.25, 0.007, 0.07)
# Microsoft Foundry, Global Standard in eastus2. gpt-oss-120b has no cached-input meter;
# Mistral Small 3.1 has no prompt caching on Foundry.
_GPT_OSS_120B_AZURE = Price(0.15, 0.60, 0.15, 0.15)
_MISTRAL_SMALL_AZURE = Price(0.10, 0.30, 0.10, 0.10)
_FREE = Price(0.0, 0.0, 0.0, 0.0)

PRICES: dict[str, Price] = {
    # first-party and Vertex IDs
    "claude-sonnet-5": _SONNET,
    "claude-opus-5": _OPUS,
    "claude-haiku-4-5": _HAIKU,
    "claude-haiku-4-5@20251001": _HAIKU,
    # Bedrock IDs
    "anthropic.claude-sonnet-5": _SONNET_BEDROCK,
    "anthropic.claude-opus-5": _OPUS,
    "anthropic.claude-haiku-4-5": _HAIKU,
    # Bedrock open-weight and Nova ids, plain and under the geo profiles
    "openai.gpt-oss-120b-1:0": _GPT_OSS_120B_BEDROCK,
    "us-gov.openai.gpt-oss-120b-1:0": _GPT_OSS_120B_BEDROCK,
    "openai.gpt-oss-20b-1:0": _GPT_OSS_20B_BEDROCK,
    "us-gov.openai.gpt-oss-20b-1:0": _GPT_OSS_20B_BEDROCK,
    "amazon.nova-micro-v1:0": _NOVA_MICRO,
    "us.amazon.nova-micro-v1:0": _NOVA_MICRO,
    "eu.amazon.nova-micro-v1:0": _NOVA_MICRO,
    # Google managed API ids
    "openai/gpt-oss-120b-maas": _GPT_OSS_120B_GOOGLE,
    "openai/gpt-oss-20b-maas": _GPT_OSS_20B_GOOGLE,
    # Microsoft Foundry deployment names (the Azure track sends the deployment name as the
    # model id); claude-opus-5 above is Anthropic's rate, which is what Foundry bills.
    "gpt-oss-120b": _GPT_OSS_120B_AZURE,
    "mistral-small-2503": _MISTRAL_SMALL_AZURE,
    # Ollama tags on the Local track
    "gpt-oss:120b": _FREE,
    "gpt-oss:20b": _FREE,
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
