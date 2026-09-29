"""Qdrant as the vector store, in memory: tenant-scoped collections, string ids, cosine hits."""

from __future__ import annotations

import pytest

from nw.platform.base import Tenant, VectorStore
from nw.platform.local import LocalVectorStore, point_id

pytestmark = pytest.mark.session06


@pytest.fixture
def store():
    pytest.importorskip("qdrant_client")
    return LocalVectorStore.in_memory()


def test_upsert_search_count_drop(store, tenant):
    assert isinstance(store, VectorStore)
    ids = ["sla-2023#1", "sla-2023#2", "refund-2024#1"]
    texts = [
        "The uptime commitment is 99.95 percent per calendar month.",
        "Credits are issued when uptime falls below the commitment.",
        "Refunds are processed within ten business days of approval.",
    ]
    meta = [{"doc": "sla-2023"}, {"doc": "sla-2023"}, {"doc": "refund-2024"}]
    assert store.upsert(tenant, "policies", ids, texts, None, meta) == 3
    assert store.count(tenant, "policies") == 3
    hits = store.search(tenant, "policies", "uptime commitment percent", k=2)
    assert [h.id for h in hits][0] == "sla-2023#1"
    assert hits[0].metadata == {"doc": "sla-2023"} and hits[0].score > hits[1].score
    store.drop(tenant, "policies")
    assert store.count(tenant, "policies") == 0
    assert store.search(tenant, "policies", "anything") == []


def test_explicit_vectors_and_tenant_isolation(store):
    alice, bob = Tenant("alice"), Tenant("bob")
    store.upsert(alice, "c", ["a"], ["alpha"], [[1.0, 0.0, 0.0]], [{}])
    store.upsert(bob, "c", ["b"], ["beta"], [[0.0, 1.0, 0.0]], [{}])
    assert store.count(alice, "c") == 1 and store.count(bob, "c") == 1
    hit = store.search(alice, "c", "", k=1, vector=[1.0, 0.0, 0.0])[0]
    assert hit.id == "a" and hit.score == pytest.approx(1.0, abs=1e-6)
    assert store.search(bob, "c", "", k=5, vector=[1.0, 0.0, 0.0])[0].id == "b"


def test_point_ids_are_stable_uuids():
    assert point_id("sla-2023#3") == point_id("sla-2023#3")
    assert point_id("sla-2023#3") != point_id("sla-2023#4")
    assert len(point_id("x")) == 36


def test_upsert_validates_lengths(store, tenant):
    with pytest.raises(ValueError):
        store.upsert(tenant, "c", ["a", "b"], ["one"], None, [{}, {}])
