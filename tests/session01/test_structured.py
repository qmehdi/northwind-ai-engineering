"""Acceptance 3: structured output recovers from one malformed reply, then fails."""

import pytest
from pydantic import BaseModel, Field

from nw.llm.errors import StructuredOutputError
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session01


class Triage(BaseModel):
    priority: str = Field(pattern=r"^P[0-3]$")
    summary: str
    needs_human: bool


GOOD = '{"priority": "P1", "summary": "Login outage for one tenant", "needs_human": true}'


async def test_valid_json_is_parsed(make_client):
    client = make_client(FakeProvider([GOOD]))
    out = await client.structured("triage this", Triage)
    assert out.priority == "P1" and out.needs_human is True


async def test_fenced_json_is_tolerated(make_client):
    client = make_client(FakeProvider(["```json\n" + GOOD + "\n```"]))
    out = await client.structured("triage this", Triage)
    assert out.summary.startswith("Login")


async def test_one_repair_round_trip(make_client):
    provider = FakeProvider(['{"priority": "urgent", "summary": "x"', GOOD])
    client = make_client(provider)

    out = await client.structured("triage this", Triage)

    assert out.priority == "P1"
    assert len(provider.calls) == 2
    repair_turns = provider.calls[1]["messages"]
    assert repair_turns[-1].role == "user"
    assert "not valid" in (repair_turns[-1].content or "")


async def test_fails_after_second_bad_reply(make_client):
    provider = FakeProvider(["not json at all", '{"priority": "P9", "summary": 1}'])
    client = make_client(provider)

    with pytest.raises(StructuredOutputError):
        await client.structured("triage this", Triage)

    assert len(provider.calls) == 2


async def test_schema_is_in_the_system_prompt(make_client):
    provider = FakeProvider([GOOD])
    client = make_client(provider)
    await client.structured("triage this", Triage, system="You triage tickets.")
    system = provider.calls[0]["system"]
    assert "You triage tickets." in system
    assert "needs_human" in system and "JSON Schema" in system
