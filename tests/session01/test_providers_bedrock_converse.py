"""The Converse translation layer, tested without any network or AWS package.

Not part of the service-layer exercise; it pins the seam that puts gpt-oss and Nova
behind the same interface as Claude (ADR 0010 in the authoring workspace)."""

from __future__ import annotations

import pytest

from nw.llm.errors import ContentFilteredError, RequestTimeout, RetryableError, TerminalError
from nw.llm.providers import bedrock_converse as bc
from nw.llm.types import Message, StopReason, ToolCall, ToolResult, ToolSpec

pytestmark = pytest.mark.session01


def test_messages_round_trip_to_converse_shape():
    msgs = [
        Message.user("hi"),
        Message.assistant("calling", [ToolCall(id="t1", name="lookup", arguments={"id": 7})]),
        Message.results([ToolResult(tool_call_id="t1", content="found", is_error=True)]),
    ]
    out = bc.to_vendor_messages(msgs)
    assert out[0] == {"role": "user", "content": [{"text": "hi"}]}
    assert out[1]["content"] == [
        {"text": "calling"},
        {"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {"id": 7}}},
    ]
    assert out[2]["content"][0]["toolResult"] == {
        "toolUseId": "t1",
        "content": [{"text": "found"}],
        "status": "error",
    }


def test_assistant_raw_blocks_are_replayed_verbatim():
    raw = [{"reasoningContent": {"reasoningText": {"text": "hmm"}}}, {"text": "ok"}]
    out = bc.to_vendor_messages([Message.assistant("ok", raw_content=raw)])
    assert out == [{"role": "assistant", "content": raw}]


def test_request_carries_system_tools_and_inference_config():
    req = bc.build_request(
        [Message.user("q")],
        model="openai.gpt-oss-120b-1:0",
        system="be brief",
        tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object"})],
        max_tokens=64,
        temperature=0.2,
    )
    assert req["modelId"] == "openai.gpt-oss-120b-1:0"
    assert req["system"] == [{"text": "be brief"}]
    assert req["inferenceConfig"] == {"maxTokens": 64, "temperature": 0.2}
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": {"type": "object"}}


def test_inference_profile_id_is_passed_through_unchanged():
    req = bc.build_request(
        [Message.user("q")],
        model="us.amazon.nova-micro-v1:0",
        system=None,
        tools=None,
        max_tokens=8,
        temperature=None,
    )
    assert req["modelId"] == "us.amazon.nova-micro-v1:0"
    assert "system" not in req and "toolConfig" not in req
    assert req["inferenceConfig"] == {"maxTokens": 8}


def _response(stop="end_turn", content=None, usage=None):
    return {
        "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
        "output": {
            "message": {
                "role": "assistant",
                "content": content
                if content is not None
                else [
                    {"text": "Let me check."},
                    {"toolUse": {"toolUseId": "tu_1", "name": "lookup", "input": {"id": 7}}},
                ],
            }
        },
        "stopReason": stop,
        "usage": usage
        or {
            "inputTokens": 10,
            "outputTokens": 5,
            "totalTokens": 15,
            "cacheReadInputTokens": 3,
            "cacheWriteInputTokens": 1,
        },
        "metrics": {"latencyMs": 40},
    }


def test_response_is_normalised():
    c = bc.from_vendor_response(_response("tool_use"), model="m", latency_ms=12.0)
    assert c.text == "Let me check."
    assert c.tool_calls[0] == ToolCall(id="tu_1", name="lookup", arguments={"id": 7})
    assert c.stop_reason is StopReason.TOOL_USE
    assert c.request_id == "req-1" and c.model == "m"
    assert (c.usage.input_tokens, c.usage.output_tokens) == (10, 5)
    assert (c.usage.cache_read_tokens, c.usage.cache_write_tokens) == (3, 1)
    assert c.usage.latency_ms == 12.0
    assert c.raw_content[1]["toolUse"]["name"] == "lookup"


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("stop_sequence", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("malformed_tool_use", StopReason.OTHER),
    ],
)
def test_stop_reasons_map(stop, expected):
    c = bc.from_vendor_response(_response(stop, content=[{"text": "x"}]), model="m", latency_ms=1.0)
    assert c.stop_reason is expected


def test_guardrail_becomes_content_filtered_error():
    with pytest.raises(ContentFilteredError) as info:
        bc.from_vendor_response(
            _response("guardrail_intervened", content=[]), model="m", latency_ms=1.0
        )
    assert info.value.completion.usage.input_tokens == 10


class _ClientError(Exception):
    """The shape of botocore's ClientError: a `response` dict, read structurally."""

    def __init__(self, code, status, message="boom", headers=None):
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": "rid",
                "HTTPHeaders": headers or {},
            },
        }


class ReadTimeoutError(Exception):
    pass


class EndpointConnectionError(Exception):
    pass


def test_errors_are_classified():
    r = bc.classify(
        _ClientError("ThrottlingException", 429, headers={"retry-after": "7"}), timeout_s=60
    )
    assert isinstance(r, RetryableError) and r.retry_after_s == 7.0 and r.status == 429
    assert r.request_id == "rid"

    r = bc.classify(_ClientError("ValidationException", 400), timeout_s=60)
    assert isinstance(r, TerminalError) and r.status == 400

    r = bc.classify(_ClientError("ResourceNotFoundException", 404, "no such model"), timeout_s=60)
    assert isinstance(r, TerminalError) and r.status == 404 and "no such model" in str(r)

    r = bc.classify(_ClientError("AccessDeniedException", 403), timeout_s=60)
    assert isinstance(r, TerminalError) and r.status == 403

    r = bc.classify(_ClientError("InternalServerException", 500), timeout_s=60)
    assert isinstance(r, RetryableError) and r.status == 500

    r = bc.classify(_ClientError("ModelTimeoutException", 408), timeout_s=60)
    assert isinstance(r, RequestTimeout) and r.timeout_s == 60

    r = bc.classify(ReadTimeoutError("read timed out"), timeout_s=30)
    assert isinstance(r, RequestTimeout) and r.timeout_s == 30

    r = bc.classify(EndpointConnectionError("no route"), timeout_s=30)
    assert isinstance(r, RetryableError) and not isinstance(r, RequestTimeout)

    r = bc.classify(ValueError("client-side"), timeout_s=30)
    assert isinstance(r, TerminalError)


def test_botocore_client_error_is_classified_when_installed():
    botocore = pytest.importorskip("botocore.exceptions")
    exc = botocore.ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "ResponseMetadata": {"HTTPStatusCode": 429, "RequestId": "r"},
        },
        "Converse",
    )
    r = bc.classify(exc, timeout_s=60)
    assert isinstance(r, RetryableError) and r.status == 429


class _FakeConverse:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_provider_round_trip_through_a_fake_client():
    client = _FakeConverse(_response("end_turn", content=[{"text": "ready"}]))
    provider = bc.BedrockConverseProvider(region="us-east-1", client=client)
    c = await provider.complete(
        [Message.user("ping")], model="us.amazon.nova-micro-v1:0", max_tokens=8
    )
    assert c.text == "ready" and c.usage.latency_ms >= 0
    assert client.calls[0]["modelId"] == "us.amazon.nova-micro-v1:0"
    assert provider.endpoint == "https://bedrock-runtime.us-east-1.amazonaws.com"


async def test_provider_raises_classified_errors():
    provider = bc.BedrockConverseProvider(
        region="us-east-1", client=_FakeConverse(_ClientError("ThrottlingException", 429))
    )
    with pytest.raises(RetryableError) as info:
        await provider.complete([Message.user("ping")], model="m")
    assert info.value.status == 429
