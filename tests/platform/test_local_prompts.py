"""Prompts as registered artifacts in MLflow, with the course's sha256_12 hash."""

from __future__ import annotations

import pytest

from nw.llm.prompts import prompt_hash
from nw.platform.base import PromptStore, Stage
from nw.platform.local import LocalPromptStore

pytestmark = pytest.mark.session06


def test_register_carries_the_prompt_hash_and_is_idempotent(mlflow_uri, tenant):
    store = LocalPromptStore(mlflow_uri)
    assert isinstance(store, PromptStore)
    text = "You are Northwind's policy assistant. Cite the policy id."
    v1 = store.register(tenant, "policy.answer", text, {"owner": "cx"})
    assert v1.sha256_12 == prompt_hash(text) and v1.version == "1"
    assert v1.stage is Stage.CANDIDATE and v1.tags["owner"] == "cx"
    again = store.register(tenant, "policy.answer", text, {})
    assert again.version == "1"
    v2 = store.register(tenant, "policy.answer", text + " Refuse when unsure.", {})
    assert v2.version == "2" and v2.sha256_12 != v1.sha256_12


def test_get_prefers_live_then_newest_and_versions_list(mlflow_uri, tenant):
    store = LocalPromptStore(mlflow_uri)
    v1 = store.register(tenant, "rubric", "score 1 to 5", {})
    v2 = store.register(tenant, "rubric", "score 1 to 5, cite evidence", {})
    assert store.get(tenant, "rubric").version == v2.version
    live = store.set_stage(tenant, "rubric", v1.version, Stage.LIVE)
    assert live.stage is Stage.LIVE
    assert store.get(tenant, "rubric").version == v1.version
    assert store.get(tenant, "rubric", v2.version).text == "score 1 to 5, cite evidence"
    assert [v.version for v in store.versions(tenant, "rubric")] == ["1", "2"]
    with pytest.raises(KeyError):
        store.get(tenant, "missing")
