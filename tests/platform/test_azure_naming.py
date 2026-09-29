"""The Azure naming rules: every generated name fits the service's pattern and length, for the
longest tenant handle the contract allows, and stays unique when it has to be cut."""

from __future__ import annotations

import re

import pytest

from nw.platform import azure
from nw.platform.base import Tenant

LONGEST = "abcdefghijklmnop"  # 16 characters, the TENANT_RE limit
ENDPOINT_RE = re.compile(r"^[a-zA-Z][-a-zA-Z0-9]*[a-zA-Z0-9]$")
INDEX_RE = re.compile(r"^[a-z0-9](?!.*--)[a-z0-9-]*[a-z0-9]$")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@pytest.mark.parametrize("tenant", ["al", "alice", LONGEST])
@pytest.mark.parametrize("kind", ["triage", "semantic"])
@pytest.mark.parametrize("owner_live", [False, True])
def test_endpoint_names_fit_32_and_the_pattern(tenant, kind, owner_live):
    scope = azure.scope_hash("sub", "rg-northwind", "northwind")
    name = azure.endpoint_name("live" if owner_live else tenant, kind, scope)
    assert 3 <= len(name) <= 32, name
    assert ENDPOINT_RE.match(name), name
    assert name.endswith(f"-{kind}-{scope}")


def test_endpoint_scope_separates_deployments():
    a = azure.scope_hash("sub", "rg-a", "northwind")
    b = azure.scope_hash("sub", "rg-b", "northwind")
    c = azure.scope_hash("sub", "rg-a", "northwind-dev")
    assert len({a, b, c}) == 3 and all(len(x) == 5 for x in (a, b, c))
    assert azure.scope_hash("SUB", "RG-A", "Northwind") == a  # Azure names are case-insensitive


@pytest.mark.parametrize(("kind", "limit"), [("storage", 24), ("keyvault", 24), ("acr", 50)])
def test_compact_names_fit_and_are_lowercase_alphanumeric(kind, limit):
    suffix = "x7k2q9"  # the Bicep uniqueString slice
    for parts in [("northwind", "st"), ("northwind-dev", LONGEST, "artifacts")]:
        name = azure.compact_name(*parts, limit=azure.LIMITS[kind], suffix=suffix)
        assert len(name) <= limit and len(name) >= 3
        assert re.fullmatch(r"[a-z0-9]+", name), name
        assert name.startswith("nw") and name.endswith(suffix)


def test_compact_names_stay_distinct_when_cut():
    a = azure.compact_name("northwind", LONGEST, "artifactsa", limit=24, suffix="abc")
    b = azure.compact_name("northwind", LONGEST, "artifactsb", limit=24, suffix="abc")
    assert a != b and len(a) == len(b) == 24


def test_compact_name_refuses_what_cannot_fit():
    with pytest.raises(ValueError):
        azure.compact_name("x", limit=4, suffix="abcd")


@pytest.mark.parametrize("tenant", ["alice", LONGEST])
def test_tenant_names(tenant):
    t = Tenant(tenant, "northwind")
    scope = azure.scope_hash("sub", "rg", "northwind")
    out = azure.names(t, scope)
    assert out["registry model (triage)"] == f"northwind-{tenant}-triage"
    assert ASSET_RE.match(out["prompt asset (policy.answer)"])
    index = out["search index"]
    assert INDEX_RE.match(index) and len(index) <= 128
    agent = out["hosted agent"]
    assert len(agent) <= 63 and re.fullmatch(r"[a-z0-9-]+", agent)
    for key in ("online endpoint (triage)", "online endpoint (semantic)", "live endpoint (triage)"):
        assert len(out[key]) <= 32


def test_search_index_name_collapses_dashes():
    assert azure.search_index_name(Tenant("alice", "northwind-dev"), "Policy--Docs") == (
        "northwind-dev-alice-policy-docs"
    )


def test_document_keys_are_url_safe():
    key = azure.document_key("policies/refunds.md:3")
    assert re.fullmatch(r"[A-Za-z0-9_=-]+", key)
