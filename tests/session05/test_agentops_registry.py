"""AgentOps: the agent registry. A card per agent, its version the one trajectories carry,
the check that fails when code moves under a card, and the push through the platform."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nw.agent import catalog as cat
from nw.agent import registry as reg
from nw.agent.loop import run_agent, scripted_completion
from nw.agent.offline import offline_registry
from nw.config import ModelRole, Settings, Track
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def agents():
    return cat.agents_in_code(ROOT / "data" / "accounts.json")


@pytest.fixture(scope="module")
def catalog():
    return cat.load(ROOT / "data" / "use_cases.yaml")


@pytest.fixture
def written(tmp_path, agents, catalog, local_settings):
    cards = tmp_path / "agents"
    for name, a in agents.items():
        reg.write_card(reg.build_card(a, catalog.get(name), local_settings), cards)
    return cards


def test_committed_cards_match_code(agents, catalog, local_settings, monkeypatch):
    monkeypatch.chdir(ROOT)
    cards = reg.load_cards(ROOT / "data" / "agents")
    assert set(cards) == set(agents)
    r = reg.check(cards, agents, catalog, local_settings)
    assert r.passed, r.problems
    for c in cards.values():
        assert c.approval["state"] == "approved" and c.prompt_hash and len(c.agent_version) == 12


async def test_card_version_is_the_version_a_trajectory_carries(
    agents, catalog, local_settings, make_client, tmp_path, monkeypatch
):
    import nw.agent.northwind as nwmod

    monkeypatch.setattr(nwmod, "ESCALATION_QUEUE", tmp_path / "escalations.jsonl")
    card = reg.build_card(agents["resolver"], catalog.get("resolver"), local_settings)
    client = make_client(FakeProvider([scripted_completion("Done.")]))
    t = await run_agent(
        "Ticket T-200001 from account NW-10000: hello",
        offline_registry(ROOT / "data" / "accounts.json"),
        client,
    )
    assert t.agent_version == card.agent_version
    assert card.models == {"workhorse": local_settings.model_for(ModelRole.WORKHORSE)}
    assert [x.name for x in card.tools] == sorted(agents["resolver"].tools)
    assert card.evaluation.cases == 15 and set(card.evaluation.tiers) == {
        "tool",
        "turn",
        "session",
        "system",
    }
    assert card.evaluation.tiers["system"]["bar"]["min_escalation_correct_rate"] == 1.0


def test_router_card_names_both_model_roles_and_the_runtime_contracts(
    agents, catalog, local_settings
):
    card = reg.build_card(agents["router"], catalog.get("router"), local_settings)
    assert set(card.models) == {"workhorse", "economy"}
    assert any("AgentCore" in e for e in card.endpoints)
    assert any("Agent Engine" in e for e in card.endpoints)
    assert next(t for t in card.tools if t.name == "escalate").requires_approval
    triage = reg.build_card(agents["triage"], catalog.get("triage"), local_settings)
    assert triage.evaluation.cases is None and "contract tests" in triage.evaluation.note


def test_check_fails_when_code_moves_under_a_card(written, agents, catalog, local_settings):
    cards = reg.load_cards(written)
    moved = dict(agents)
    p = agents["policy"]
    moved["policy"] = cat.AgentDefinition(
        p.name, p.kind, p.system + "\nBe brief.", p.specs, p.roles, p.endpoints
    )
    r = reg.check(cards, moved, catalog, local_settings)
    assert not r.passed
    assert any(x.startswith("policy: prompt hash") for x in r.problems)
    assert any(x.startswith("policy: agent_version") for x in r.problems)
    assert not any(x.startswith("triage") for x in r.problems)


def test_check_names_missing_and_stale_cards(written, agents, catalog, local_settings):
    (written / "triage.json").unlink()
    (written / "ghost.json").write_text((written / "policy.json").read_text())
    r = reg.check(reg.load_cards(written), agents, catalog, local_settings)
    assert any("triage: no card" in x for x in r.problems)
    assert any("ghost: a card with no agent in code" in x for x in r.problems)


def test_check_notes_a_different_track_instead_of_failing(written, agents, catalog):
    aws = Settings(track=Track.AWS, model_workhorse="another-model", _env_file=None)
    r = reg.check(reg.load_cards(written), agents, catalog, aws)
    assert r.passed, r.problems
    assert any("written on the local track" in n for n in r.notes)
    assert any("another-model" in n for n in r.notes)


def test_push_goes_through_the_platform_runtime(written, monkeypatch):
    import nw.platform.base as base

    registered = []

    class Runtime:
        def register(self, tenant, card):
            registered.append((tenant.prefix, card["name"], card["agent_version"]))
            return f"arn:fake:{card['name']}"

    class Fake:
        agents = Runtime()

    monkeypatch.setattr(base, "platform_for", lambda s: Fake())
    s = Settings(track=Track.AWS, tenant="alice", _env_file=None)
    card = reg.load_cards(written)["router"]
    assert reg.push(card, s, written) == "arn:fake:router"
    assert registered == [("northwind-alice", "router", card.agent_version)]
    assert json.loads((written / "router.json").read_text())["runtime_id"] == "arn:fake:router"


def test_cli(written, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("NW_TRACK", raising=False)
    monkeypatch.delenv("NW_TENANT", raising=False)
    assert reg.main(["--cards", str(written), "--list"]) == 0
    out = capsys.readouterr().out
    assert "| resolver |" in out and "| router |" in out
    assert reg.main(["--cards", str(written), "--show", "policy"]) == 0
    assert json.loads(capsys.readouterr().out)["use_case"] == "uc-policy"
    assert reg.main(["--cards", str(written), "--show", "nobody"]) == 1
    assert reg.main(["--cards", str(written), "--check"]) == 0
    assert capsys.readouterr().out.startswith("AGENT REGISTRY OK")
    assert reg.main(["--cards", str(written), "--push"]) == 1  # no track configured
    assert "stay on disk" in capsys.readouterr().err
    assert reg.main(["--cards", str(written), "--write", "triage"]) == 0
    assert "triage:" in capsys.readouterr().out
