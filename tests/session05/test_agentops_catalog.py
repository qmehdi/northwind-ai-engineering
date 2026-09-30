"""AgentOps: the use case catalog and the lifecycle roles. Every agent in code has an
approved use case whose allowed tools cover what the agent can see; the roles map names
an owner for every lifecycle stage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nw.agent import catalog as cat
from nw.llm.types import ToolSpec

pytestmark = pytest.mark.session05

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "data" / "use_cases.yaml"


@pytest.fixture(scope="module")
def agents():
    return cat.agents_in_code(ROOT / "data" / "accounts.json")


@pytest.fixture(scope="module")
def catalog():
    return cat.load(CATALOG)


def test_every_shipped_agent_has_an_approved_use_case(catalog, agents):
    assert set(agents) == {"resolver", "triage", "policy", "resolution", "orchestrator", "router"}
    r = cat.check(catalog, agents)
    assert r.passed, r.problems
    assert any("uc-refund" in n and "not built" in n for n in r.notes)
    for name, a in agents.items():
        uc = catalog.get(name)
        assert uc is not None and uc.approval is cat.Approval.APPROVED and uc.approver
        assert set(a.tools) <= set(uc.allowed_tools)


def test_specialists_see_only_their_subset(agents):
    assert agents["policy"].tools == ["search_policies"]
    assert agents["resolution"].tools == ["escalate", "find_similar_tickets"]
    assert agents["orchestrator"].tools == ["ask_policy", "ask_resolution", "ask_triage"]
    assert len(agents["resolver"].tools) == 7 and agents["router"].roles == ("workhorse", "economy")
    assert "escalate" in agents["resolver"].requires_approval


def test_a_tool_outside_the_allowed_list_fails_the_check(catalog, agents):
    policy = agents["policy"]
    widened = cat.AgentDefinition(
        policy.name,
        policy.kind,
        policy.system,
        [*policy.specs, ToolSpec(name="escalate", description="x", input_schema={})],
        policy.roles,
        policy.endpoints,
        frozenset({"escalate"}),
    )
    r = cat.check(catalog, {"policy": widened})
    assert not r.passed
    assert any("outside uc-policy's allowed list: escalate" in p for p in r.problems)
    assert any("low risk but sees an irreversible tool" in p for p in r.problems)


def test_an_agent_without_a_use_case_or_without_approval_fails(catalog, agents):
    r = cat.check(catalog, {"newagent": agents["policy"]})
    assert not r.passed and "newagent: no use case" in r.problems[0]
    pending = catalog.model_copy(deep=True)
    pending.get("triage").approval = cat.Approval.PENDING
    r = cat.check(pending, {"triage": agents["triage"]})
    assert not r.passed and "pending, not approved" in r.problems[0]


def test_use_case_validation():
    base = dict(
        id="uc-x",
        agent="x",
        owner="product_owner",
        business_outcome="A sentence long enough to count.",
        risk_class="medium",
        allowed_tools=["search_policies"],
        data_classes=["ticket_text"],
    )
    gov = dict(
        eu_ai_act_class="minimal", dpia_ref="docs/governance/dpia.md#uc-x", privacy_approver="p"
    )
    assert cat.UseCase(**base).approval is cat.Approval.DRAFT
    with pytest.raises(ValidationError, match="approved without an approver"):
        cat.UseCase(**base, **gov, approval="approved")
    with pytest.raises(ValidationError, match="co_approver"):
        cat.UseCase(**{**base, "risk_class": "high"}, **gov, approval="approved", approver="a")
    cat.UseCase(
        **{**base, "risk_class": "high"},
        **gov,
        approval="approved",
        approver="a",
        co_approver="b",
    )
    with pytest.raises(ValidationError, match="not a lifecycle role"):
        cat.UseCase(**{**base, "owner": "wizard"})
    with pytest.raises(ValidationError, match="unknown data classes"):
        cat.UseCase(**{**base, "data_classes": ["secrets"]})
    with pytest.raises(ValidationError, match="same agent"):
        cat.Catalog(use_cases=[cat.UseCase(**base), cat.UseCase(**{**base, "id": "uc-y"})])


def test_oversight_follows_the_risk_class(catalog):
    assert catalog.get("triage").oversight["actions"] == "read-only tools"
    assert "one reviewer" in catalog.get("resolution").oversight["human"]
    assert cat.OVERSIGHT[cat.RiskClass.HIGH]["approvers"] == "two"


def test_roles_cover_every_stage_and_every_role_owns_a_step():
    owned = {stage for r in cat.ROLES.values() for stage in r.owns}
    assert owned == set(cat.STAGES)
    assert all(r.owns for r in cat.ROLES.values())
    assert set(cat.ROLES) == {
        "product_owner",
        "domain_expert",
        "platform_engineer",
        "developer",
        "data_engineer",
        "qa_engineer",
        "reviewer",
        "end_user",
    }
    table = cat.format_roles()
    assert table.startswith("| Role | Stage | Owns |") and "Test and release" in table
    j = cat.roles_json()
    assert [s["id"] for s in j["stages"]] == list(cat.STAGES)
    assert j["roles"]["qa_engineer"]["owns"]["test_and_release"].startswith("runs the tiered gate")


def test_no_dashes_in_anything_that_ships(catalog):
    text = CATALOG.read_text() + cat.format_roles() + cat.format_table(catalog)
    text += json.dumps(cat.roles_json())
    assert "\u2014" not in text and "\u2013" not in text


def test_cli(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert cat.main(["--check"]) == 0
    assert capsys.readouterr().out.startswith("USE CASE CATALOG OK")
    assert cat.main(["--roles", "--json"]) == 0
    assert "product_owner" in json.loads(capsys.readouterr().out)["roles"]
    assert cat.main([]) == 0
    assert "| uc-resolver | resolver |" in capsys.readouterr().out


def test_governance_fields_are_enforced():
    base = dict(
        id="uc-x",
        agent="x",
        owner="product_owner",
        business_outcome="A sentence long enough to count.",
        risk_class="medium",
        allowed_tools=["search_policies"],
        data_classes=["ticket_text"],
        approval="approved",
        approver="support-operations",
        eu_ai_act_class="minimal",
        dpia_ref="docs/governance/dpia.md#uc-x",
        privacy_approver="privacy-office",
    )
    cat.UseCase(**base)
    for field, message in [
        ("eu_ai_act_class", "without an eu_ai_act_class"),
        ("dpia_ref", "without a dpia_ref"),
        ("privacy_approver", "without a privacy_approver"),
    ]:
        with pytest.raises(ValidationError, match=message):
            cat.UseCase(**{**base, field: None})
    with pytest.raises(ValidationError, match="must not be the business approver"):
        cat.UseCase(**{**base, "privacy_approver": "support-operations"})
    with pytest.raises(ValidationError, match="never approved"):
        cat.UseCase(**{**base, "eu_ai_act_class": "prohibited"})
    with pytest.raises(ValidationError, match="annex_iii_item"):
        cat.UseCase(**{**base, "eu_ai_act_class": "high_risk", "risk_class": "high"})
    with pytest.raises(ValidationError, match="risk_class high"):
        cat.UseCase(**{**base, "eu_ai_act_class": "high_risk", "annex_iii_item": "4(a)"})
    with pytest.raises(ValidationError, match="only for high_risk"):
        cat.UseCase(**{**base, "annex_iii_item": "4(a)"})
    with pytest.raises(ValidationError, match="article_50_disclosure"):
        cat.UseCase(**{**base, "eu_ai_act_class": "transparency"})
    cat.UseCase(**{**base, "eu_ai_act_class": "transparency", "article_50_disclosure": "AI."})


def test_every_approved_use_case_resolves_its_dpia_section(catalog):
    for uc in catalog.use_cases:
        assert uc.dpia_ref and cat.dpia_problem(uc.dpia_ref) is None, uc.id
    assert "no section" in cat.dpia_problem("docs/governance/dpia.md#uc-nowhere")
    assert "no file" in cat.dpia_problem("docs/governance/missing.md#uc-x")


def test_reach_is_derived_from_tools_and_delegates(catalog, agents):
    reach, undeclared = cat.reachable_classes("orchestrator", agents)
    assert not undeclared
    assert reach == cat.DATA_CLASSES  # the specialists' reach flows back through ask_*
    assert cat.reachable_classes("policy", agents)[0] == {"ticket_text", "policy_corpus"}
    narrow = catalog.model_copy(deep=True)
    narrow.get("orchestrator").data_classes = ["ticket_text"]
    r = cat.check(narrow, {"orchestrator": agents["orchestrator"], **agents})
    assert any(
        "orchestrator: reaches data classes uc-orchestrator does not declare" in p
        for p in r.problems
    )


def test_a_tool_with_no_declared_outputs_fails_the_check(catalog, agents):
    policy = agents["policy"]
    widened = cat.AgentDefinition(
        policy.name,
        policy.kind,
        policy.system,
        [*policy.specs, ToolSpec(name="read_mailbox", description="x", input_schema={})],
        policy.roles,
        policy.endpoints,
    )
    r = cat.check(catalog, {"policy": widened})
    assert any("no declared output data classes: read_mailbox" in p for p in r.problems)
    declared = cat.AgentDefinition(
        policy.name,
        policy.kind,
        policy.system,
        policy.specs,
        policy.roles,
        policy.endpoints,
        outputs={"search_policies": frozenset({"customer_account"})},
    )
    r = cat.check(catalog, {"policy": declared})
    assert any("does not declare: customer_account" in p for p in r.problems)
