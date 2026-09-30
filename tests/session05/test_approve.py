"""AgentOps: approve. The operator lists pending proposals and approves exactly one; the
recorded action executes once, as proposed, with no second model call; the approval is its
own trace pointing at the parent. A second approval is refused by the claim marker, and
nobody approves their own request. The router's P0 proposal is approvable the same way."""

import asyncio
import json

import pytest

from nw.agent.approve import (
    AlreadyApproved,
    SelfApproval,
    approve,
    approve_exactly,
    list_proposals,
)
from nw.agent.loop import run_agent, scripted_completion
from nw.agent.opstore import LocalStore
from nw.agent.router import route
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
    t = await run_agent(
        TASK,
        registry,
        make_client(_propose()),
        max_steps=6,
        account_id="NW-10000",
        requested_by="key:cohort-a",
    )
    assert t.proposed_actions and t.proposed_actions[0].tool == "escalate"
    t.save(tmp_path / "traces")
    return t


def test_approve_exactly_matches_tool_and_arguments():
    policy = approve_exactly("escalate", PROPOSAL)
    assert policy("escalate", dict(PROPOSAL))
    assert not policy("escalate", {**PROPOSAL, "tier": "billing"})
    assert not policy("lookup_customer", PROPOSAL)


async def test_list_then_approve_executes_exactly_the_recorded_action(
    parent, registry, tmp_path, escalation_file
):
    rows = list_proposals(tmp_path / "traces")
    assert [(r["run_id"], r["tool"], r["step"]) for r in rows] == [(parent.run_id, "escalate", 1)]
    assert rows[0]["arguments"] == PROPOSAL and rows[0]["task"].startswith("Ticket T-200005")
    assert rows[0]["requested_by"] == "key:cohort-a"
    assert not escalation_file.exists()

    t = await approve(
        parent.run_id, "escalate", registry, traces=tmp_path / "traces", approved_by="ops@nw"
    )
    assert t.resumed_from == parent.run_id and t.approved_by == "ops@nw"
    assert t.tools_called == ["escalate"] and t.steps[0].ok and not t.proposed_actions
    (line,) = escalation_file.read_text().splitlines()
    assert json.loads(line)["justification"] == PROPOSAL["justification"]
    saved = Trajectory.load(tmp_path / "traces" / f"{t.run_id}.json")
    assert saved.resumed_from == parent.run_id and "resumed_from" in replay(saved)
    assert list_proposals(tmp_path / "traces") == []  # claimed, so no longer pending
    records = LocalStore(tmp_path / "approvals").keys("records/")
    assert len(records) == 1


async def test_a_second_approval_is_refused_by_the_claim_marker(
    parent, registry, tmp_path, escalation_file
):
    kw = {"traces": tmp_path / "traces", "approved_by": "ops@nw"}
    await approve(parent.run_id, "escalate", registry, **kw)
    with pytest.raises(AlreadyApproved):
        await approve(parent.run_id, "escalate", registry, **kw)
    assert len(escalation_file.read_text().splitlines()) == 1


async def test_concurrent_approvals_execute_once(parent, registry, tmp_path, escalation_file):
    kw = {"traces": tmp_path / "traces"}
    results = await asyncio.gather(
        approve(parent.run_id, "escalate", registry, approved_by="a@nw", **kw),
        approve(parent.run_id, "escalate", registry, approved_by="b@nw", **kw),
        return_exceptions=True,
    )
    assert sum(isinstance(r, AlreadyApproved) for r in results) == 1
    assert len(escalation_file.read_text().splitlines()) == 1


async def test_nobody_approves_their_own_request(parent, registry, tmp_path, escalation_file):
    with pytest.raises(SelfApproval):
        await approve(
            parent.run_id,
            "escalate",
            registry,
            traces=tmp_path / "traces",
            approved_by="key:cohort-a",
        )
    assert not escalation_file.exists()
    assert list_proposals(tmp_path / "traces"), "a refused approval does not claim the proposal"


async def test_approve_refuses_a_tool_that_was_not_proposed(parent, registry, tmp_path):
    with pytest.raises(LookupError, match="no pending proposal"):
        await approve(
            parent.run_id,
            "lookup_customer",
            registry,
            traces=tmp_path / "traces",
            approved_by="ops@nw",
        )


async def test_the_router_p0_proposal_is_approvable(
    registry, make_client, tmp_path, escalation_file
):
    """No model on the P0 branch, so no model can reproduce the justification on a resume:
    executing the recorded action is the only way this proposal can ever be approved."""
    client = make_client(FakeProvider([scripted_completion("never called")]))
    t = await route("T-200001", "NW-10000", "Production down", "The API is down.", registry, client)
    assert t.agent == "router" and t.proposed_actions
    t.save(tmp_path / "traces")
    done = await approve(
        t.run_id, "escalate", registry, traces=tmp_path / "traces", approved_by="ops@nw"
    )
    assert done.steps[0].ok and '"tier": "duty_manager"' in escalation_file.read_text()
