"""The Messages API request the provider sends: cache breakpoints on the stable prefix, no
sampling parameters for models that reject them, and the model's own blocks replayed."""

import pytest

from nw.llm.providers.anthropic_base import build_request, rejects_sampling
from nw.llm.types import Message, ToolCall, ToolSpec

pytestmark = pytest.mark.session01

TOOLS = [
    ToolSpec(name="a", description="first", input_schema={"type": "object"}),
    ToolSpec(name="b", description="second", input_schema={"type": "object"}),
]


def test_system_and_last_tool_carry_cache_breakpoints():
    req = build_request(
        [Message.user("hi")],
        model="claude-sonnet-5",
        system="rules",
        tools=TOOLS,
        max_tokens=10,
        temperature=None,
    )
    assert req["system"] == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in req["tools"][0]
    assert req["tools"][1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5",
        "anthropic.claude-opus-5",
        "global.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-5",
    ],
)
def test_sampling_dropped_for_models_that_reject_it(model):
    assert rejects_sampling(model) or model.endswith("haiku-5")
    req = build_request(
        [Message.user("hi")], model=model, system=None, tools=None, max_tokens=10, temperature=0.2
    )
    if rejects_sampling(model):
        assert "temperature" not in req


def test_assistant_turn_replays_the_models_own_blocks():
    raw = [
        {"type": "thinking", "thinking": "consider the tools", "signature": "sig"},
        {"type": "tool_use", "id": "c1", "name": "a", "input": {}},
    ]
    turn = Message.assistant("", [ToolCall(id="c1", name="a", arguments={})], raw_content=raw)
    req = build_request(
        [Message.user("go"), turn],
        model="claude-sonnet-5",
        system=None,
        tools=TOOLS,
        max_tokens=10,
        temperature=None,
    )
    assert req["messages"][1] == {"role": "assistant", "content": raw}


def test_assistant_turn_without_raw_blocks_is_rebuilt():
    turn = Message.assistant("text", [ToolCall(id="c1", name="a", arguments={"x": 1})])
    req = build_request(
        [Message.user("go"), turn],
        model="claude-sonnet-5",
        system=None,
        tools=None,
        max_tokens=10,
        temperature=None,
    )
    assert req["messages"][1]["content"] == [
        {"type": "text", "text": "text"},
        {"type": "tool_use", "id": "c1", "name": "a", "input": {"x": 1}},
    ]
