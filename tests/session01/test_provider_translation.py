"""The vendor translation layer, tested without any network.

These are not part of the first part's exercise; they pin the seam that keeps
vendors swappable (ADR 0002 in the authoring workspace)."""

from types import SimpleNamespace

import pytest

from nw.llm.errors import ContentFilteredError, RetryableError, TerminalError
from nw.llm.providers import anthropic_base as ab
from nw.llm.types import Message, StopReason, ToolCall, ToolResult

pytestmark = pytest.mark.session01


def test_messages_round_trip_to_vendor_shape():
    msgs = [
        Message.user("hi"),
        Message.assistant("calling", [ToolCall(id="t1", name="lookup", arguments={"id": 7})]),
        Message.results([ToolResult(tool_call_id="t1", content="found")]),
    ]
    out = ab.to_vendor_messages(msgs)
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "lookup",
        "input": {"id": 7},
    }
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "t1"


def test_response_is_normalised():
    response = SimpleNamespace(
        id="msg_1",
        model="claude-sonnet-5",
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="Let me check."),
            SimpleNamespace(type="tool_use", id="tu_1", name="lookup", input={"id": 7}),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=0,
        ),
    )
    c = ab.from_vendor_response(response, latency_ms=12.0)
    assert c.text == "Let me check."
    assert c.tool_calls[0].name == "lookup"
    assert c.stop_reason is StopReason.TOOL_USE
    assert c.usage.cache_read_tokens == 3


def test_refusal_becomes_content_filtered_error():
    response = SimpleNamespace(
        id="msg_2",
        model="m",
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber"),
        content=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
    )
    with pytest.raises(ContentFilteredError):
        ab.from_vendor_response(response, latency_ms=1.0)


def test_sdk_errors_are_classified():
    import httpx
    from anthropic import BadRequestError, InternalServerError, RateLimitError

    req = httpx.Request("POST", "https://example.invalid/v1/messages")

    def resp(status, headers=None):
        return httpx.Response(status, request=req, headers=headers or {})

    r = ab.classify(
        RateLimitError("slow down", response=resp(429, {"retry-after": "7"}), body=None)
    )
    assert isinstance(r, RetryableError) and r.retry_after_s == 7.0 and r.status == 429

    r = ab.classify(InternalServerError("boom", response=resp(500), body=None))
    assert isinstance(r, RetryableError)

    r = ab.classify(BadRequestError("nope", response=resp(400), body=None))
    assert isinstance(r, TerminalError) and r.status == 400


def test_sampling_is_dropped_for_models_that_reject_it():
    assert ab.rejects_sampling("anthropic.claude-sonnet-5")
    assert ab.rejects_sampling("claude-opus-5")
    assert not ab.rejects_sampling("claude-haiku-4-5@20251001")
