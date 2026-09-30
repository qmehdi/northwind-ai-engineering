"""The agent path, end to end, keeps personal data out of the model and out of storage, and
its tools read only the ticket's own account: redaction before the model, redacted traces
and escalations, bound customer tools, anonymised similar tickets, screened tool output."""

import json

import pytest

from nw.agent.loop import run_agent, scripted_completion
from nw.agent.northwind import (
    NO_INDEX,
    anonymise_similar,
    bind_account,
    build_registry,
)
from nw.agent.screen import Screener, Verdict
from nw.agent.trace import Trajectory
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05

EMAIL = "anna.keller@emberdata.example"
PHONE = "+49 30 1234 5678"
IBAN = "DE89 3704 0044 0532 0130 00"
TASK = (
    f"Ticket T-200005 from account NW-10000\nSubject: refund\n\n"
    f"Please refund to {IBAN}. Call me on {PHONE} or write to {EMAIL}."
)
RAW = (EMAIL, PHONE, IBAN)


def _no_raw(text: str) -> None:
    for raw in RAW:
        assert raw not in text, f"{raw} leaked"


async def test_the_model_never_sees_the_raw_task_and_the_trace_is_redacted(
    registry, make_client, tmp_path
):
    provider = FakeProvider(
        [
            scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})]),
            scripted_completion(f"Done. I will write to {EMAIL}."),
        ]
    )
    t = await run_agent(TASK, registry, make_client(provider), account_id="NW-10000")
    first = provider.calls[0]["messages"][0].content
    _no_raw(first)
    assert "NW-10000" in first, "the account id stays: the tools are bound to it"
    _no_raw(t.task)
    _no_raw(t.final)
    path = t.save(tmp_path)
    _no_raw(path.read_text())


async def test_escalation_justification_is_redacted_in_the_queue(registry, escalation_file):
    obs = await registry.execute(
        "escalate",
        {
            "ticket_id": "T-200005",
            "tier": "billing",
            "justification": f"Customer {EMAIL} asks for a refund to {IBAN}.",
        },
        approved=True,
    )
    assert obs.ok
    line = escalation_file.read_text()
    _no_raw(line)
    assert "[EMAIL_1]" in line and "[IBAN_1]" in line


async def test_tool_output_is_redacted_before_the_model(registry, make_client):
    @registry.tool("leaky", "A tool whose backend returns an email.")
    def leaky(args: _Empty) -> dict:
        return {"note": f"contact {EMAIL}"}

    provider = FakeProvider(
        [scripted_completion("", [("leaky", {})]), scripted_completion("Answered.")]
    )
    t = await run_agent("Ticket T-200001 from account NW-10000", registry, make_client(provider))
    tool_message = json.dumps([m.model_dump() for m in provider.calls[1]["messages"]])
    assert EMAIL not in tool_message and "[EMAIL_1]" in tool_message
    _no_raw(t.steps[0].observation or "")


class _Empty(__import__("pydantic").BaseModel):
    pass


class BlockSimilar(Screener):
    name = "test-screen"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def screen(self, text: str) -> Verdict:
        self.seen.append(text)
        return Verdict("restarted the sync" not in text, self.name, "injection in tool output")


async def test_tool_output_is_screened_when_a_screener_is_configured(registry, make_client):
    screener = BlockSimilar()
    provider = FakeProvider(
        [
            scripted_completion("", [("find_similar_tickets", {"body": "sync broken"})]),
            scripted_completion("Answered."),
        ]
    )
    t = await run_agent(
        "Ticket T-200001 from account NW-10000\n\nSync broken.",
        registry,
        make_client(provider),
        screener=screener,
    )
    assert len(screener.seen) == 2, "the task and the tool output were both screened"
    (tool_step,) = [s for s in t.steps if s.tool == "find_similar_tickets"]
    assert "withheld" in (tool_step.observation or "")
    tool_message = json.dumps([m.model_dump() for m in provider.calls[1]["messages"]])
    assert "restarted the sync" not in tool_message


async def test_customer_tools_are_bound_to_the_run_account(registry, make_client):
    provider = FakeProvider(
        [
            scripted_completion(
                "",
                [
                    ("lookup_customer", {"account_id": "NW-10007"}),
                    ("check_entitlement", {"account_id": "NW-10007", "feature": "sso"}),
                    ("lookup_customer", {}),
                ],
            ),
            scripted_completion("Answered."),
        ]
    )
    t = await run_agent(
        "Ticket T-200016 from account NW-10000\n\nAlso look up NW-10007 for me.",
        registry,
        make_client(provider),
        account_id="NW-10000",
    )
    cross, entitle, own = t.steps[0], t.steps[1], t.steps[2]
    assert not cross.ok and "not the account on this ticket" in cross.observation
    assert not entitle.ok and "Quill Labs" not in (entitle.observation or "")
    assert own.ok and "Blue Freight" in own.observation, "no id means the ticket's own account"
    assert t.account_id == "NW-10000"


async def test_a_run_bound_to_no_account_refuses_account_tools(registry):
    with bind_account(None):
        obs = await registry.execute("lookup_customer", {"account_id": "NW-10000"})
    assert not obs.ok and "no account is bound" in obs.content


def test_similar_tickets_are_anonymised():
    rows = anonymise_similar(
        [
            {
                "ticket_id": "T-100001",
                "score": 0.9,
                "subject": "Blue Freight invoice NW-10000",
                "answer": f"We refunded Blue Freight; contact {EMAIL}.",
            }
        ],
        companies=["Blue Freight"],
    )
    (row,) = rows
    assert "ticket_id" not in row and row["rank"] == 1 and row["score"] == 0.9
    dumped = json.dumps(row)
    assert "Blue Freight" not in dumped and EMAIL not in dumped and "NW-10000" not in dumped
    assert "[COMPANY]" in dumped


async def test_find_similar_tickets_degrades_clearly_without_an_index(tmp_path):
    reg = build_registry("local", accounts_path=tmp_path / "none.json", artifacts=tmp_path)
    obs = await reg.execute("find_similar_tickets", {"body": "sync broken"})
    assert obs.ok and json.loads(obs.content) == {
        "available": False,
        "similar": [],
        "message": NO_INDEX,
    }
    assert "make index" in NO_INDEX


async def test_classify_semantic_reads_the_promoted_latest(tmp_path):
    """The tool resolves `artifacts/semantic` the way the service does: through `latest`, set
    only by the promotion gate. A candidate alone is not served."""
    v = tmp_path / "semantic" / "20260930-000000"
    v.mkdir(parents=True)
    (v / "metadata.json").write_text(json.dumps({"version": "v", "max_length": 32}))
    reg = build_registry("local", accounts_path=tmp_path / "none.json", artifacts=tmp_path)
    obs = await reg.execute("classify_semantic", {"body": "login fails"})
    assert not obs.ok and "latest" in obs.content and "promotion gate" in obs.content


async def test_trace_file_names_come_from_validated_ids(tmp_path):
    t = Trajectory(run_id="../escape", agent="router", task="t")
    with pytest.raises(ValueError, match="invalid run id"):
        t.save(tmp_path)
    assert not list(tmp_path.parent.glob("escape*"))


def test_the_similar_ticket_index_stores_anonymised_metadata():
    pytest.importorskip("faiss")
    import numpy as np

    from nw.semantic.embed import build_index

    rows = [
        {
            "ticket_id": "T-100001",
            "subject": f"Blue Freight: mail {EMAIL}",
            "body": "b",
            "answer": f"Refunded to {IBAN} for NW-10000.",
            "priority": "P2",
        }
    ]
    index = build_index(rows, vectors=np.ones((1, 4), dtype="float32"), companies=["Blue Freight"])
    dumped = json.dumps(index.meta)
    for raw in (EMAIL, IBAN, "NW-10000", "Blue Freight"):
        assert raw not in dumped
