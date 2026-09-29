"""The prompt catalog the CDK stack reads must match the registry in the repo."""

from __future__ import annotations

import json

from nw.platform.aws import CATALOG, prompts_catalog


def test_catalog_on_disk_matches_the_registry():
    assert CATALOG.exists(), "run `uv run python -m nw.platform.aws prompts-catalog`"
    on_disk = json.loads(CATALOG.read_text())
    assert on_disk == prompts_catalog(), "stale catalog: run `make prompts-catalog`"
    assert {row["name"] for row in on_disk} >= {
        "policy.answer",
        "policy.judge",
        "agent.rules",
        "llm.structured",
    }
    for row in on_disk:
        assert len(row["sha256_12"]) == 12 and row["text"]
