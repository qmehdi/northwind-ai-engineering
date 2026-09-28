"""AgentOps: approve and resume. The operator lists pending proposals, approves exactly one,
the case re-runs with that approval, the queue line is written and the new trace points at
the parent. A reworded proposal is a new proposal, not an execution."""

import pytest

from nw.agent.approve import approve_exactly, list_proposals, resume
from nw.agent.loop import run_agent, scripted_completion
from nw.agent.trace import Trajectory, replay
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

TASK = "Ticket T-200005 from account NW-10000\nSubject: production down\n\nThe API is down."
PROPOSAL = {
    "ticket_id": "T-200005",
    "tier": "engineering",
    "justification": "Enterprise API outage for all users, P0 per triage.",
}


def _propose(justification=PROPOSAL["justification"]):
    return FakeProvider(
        [
            scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})]),
            scripted_completion("", [("escalate", {**PROPOSAL, "justification": justification})]),
            scripted_completion("Escalation handled."),
        ]
    )


@pytest.fixture
async def parent(registry, make_client, tmp_path):
    t = await run_agent(TASK, registry, make_client(_propose()), max_steps=6)
    assert t.proposed_actions and t.proposed_actions[0].tool == "escalate"
    t.save(tmp_path / "traces")
    return t


def test_approve_exactly_matches_tool_and_arguments():
    policy = approve_exactly("escalate", PROPOSAL)
    assert policy("escalate", dict(PROPOSAL))
    assert not policy("escalate", {**PROPOSAL, "tier": "billing"})
    assert not policy("lookup_customer", PROPOSAL)


async def test_list_then_resume_executes_only_the_approved_action(
    parent, registry, make_client, tmp_path, escalation_file
):
    rows = list_proposals(tmp_path / "traces")
    assert [(r["run_id"], r["tool"], r["step"]) for r in rows] == [(parent.run_id, "escalate", 1)]
    assert rows[0]["arguments"] == PROPOSAL and rows[0]["task"].startswith("Ticket T-200005")
    assert not escalation_file.exists()

    t = await resume(
        parent.run_id,
        "escalate",
        registry,
        make_client(_propose()),
        traces=tmp_path / "traces",
        approved_by="tests",
    )
    assert t.resumed_from == parent.run_id and not t.proposed_actions
    assert escalation_file.exists() and '"tier": "engineering"' in escalation_file.read_text()
    assert t.agent_version == parent.agent_version
    saved = Trajectory.load(tmp_path / "traces" / f"{t.run_id}.json")
    assert saved.resumed_from == parent.run_id and "resumed_from" in replay(saved)
    assert list_proposals(tmp_path / "traces") == []  # the parent is resolved, the child clean


async def test_a_reworded_proposal_is_not_executed(
    parent, registry, make_client, tmp_path, escalation_file
):
    t = await resume(
        parent.run_id,
        "escalate",
        registry,
        make_client(_propose("Different words this time, same tier and ticket.")),
        traces=tmp_path / "traces",
    )
    assert t.proposed_actions and not escalation_file.exists()
    assert t.resumed_from == parent.run_id


async def test_resume_refuses_a_tool_that_was_not_proposed(parent, registry, make_client, tmp_path):
    with pytest.raises(LookupError, match="no pending proposal"):
        await resume(
            parent.run_id,
            "lookup_customer",
            registry,
            make_client(_propose()),
            traces=tmp_path / "traces",
        )
