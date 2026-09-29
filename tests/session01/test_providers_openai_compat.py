"""The OpenAI-compatible translation layer (Ollama, the gateway, Google's managed API),
tested against an httpx mock transport. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from nw.llm.errors import ContentFilteredError, RequestTimeout, RetryableError, TerminalError
from nw.llm.providers import openai_compat as oc
from nw.llm.types import Message, StopReason, ToolCall, ToolResult, ToolSpec

pytestmark = pytest.mark.session01


def test_messages_round_trip_to_chat_completions_shape():
    msgs = [
        Message.user("hi"),
        Message.assistant("calling", [ToolCall(id="t1", name="lookup", arguments={"id": 7})]),
        Message.results(
            [
                ToolResult(tool_call_id="t1", content="found"),
                ToolResult(tool_call_id="t2", content="nope", is_error=True),
            ]
        ),
    ]
    out = oc.to_vendor_messages(msgs, system="be brief")
    assert out[0] == {"role": "system", "content": "be brief"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2]["role"] == "assistant" and out[2]["content"] == "calling"
    call = out[2]["tool_calls"][0]
    assert call["id"] == "t1" and call["type"] == "function"
    assert call["function"] == {"name": "lookup", "arguments": json.dumps({"id": 7})}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "found"}
    assert out[4] == {"role": "tool", "tool_call_id": "t2", "content": "error: nope"}


def test_request_carries_tools_and_sampling():
    body = oc.build_request(
        [Message.user("q")],
        model="gpt-oss:120b",
        system=None,
        tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object"})],
        max_tokens=64,
        temperature=0.1,
    )
    assert body["model"] == "gpt-oss:120b" and body["max_tokens"] == 64
    assert body["temperature"] == 0.1 and body["stream"] is False
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    assert body["messages"] == [{"role": "user", "content": "q"}]


def _body(finish="stop", message=None, usage=None, model="gpt-oss:120b"):
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": message or {"role": "assistant", "content": "ready"},
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    }


def test_response_is_normalised_with_tool_calls():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": json.dumps({"id": 7})},
            },
            {"type": "function", "function": {"name": "raw", "arguments": "{not json"}},
        ],
    }
    c = oc.from_vendor_response(_body("tool_calls", message), model="m", latency_ms=9.0)
    assert c.text == ""
    assert c.tool_calls[0] == ToolCall(id="call_1", name="lookup", arguments={"id": 7})
    assert c.tool_calls[1].id == "call_1" or c.tool_calls[1].id.startswith("call_")
    assert c.tool_calls[1].arguments == {"_raw": "{not json"}
    assert c.stop_reason is StopReason.TOOL_USE
    assert c.request_id == "chatcmpl-1" and c.model == "gpt-oss:120b"
    assert (c.usage.input_tokens, c.usage.output_tokens, c.usage.cache_read_tokens) == (10, 5, 3)
    assert c.usage.latency_ms == 9.0


@pytest.mark.parametrize(
    ("finish", "expected"),
    [("stop", StopReason.END_TURN), ("length", StopReason.MAX_TOKENS), ("odd", StopReason.OTHER)],
)
def test_finish_reasons_map(finish, expected):
    assert oc.from_vendor_response(_body(finish), model="m", latency_ms=1).stop_reason is expected


def test_content_parts_are_joined():
    message = {"role": "assistant", "content": [{"type": "text", "text": "a"}, {"text": "b"}]}
    assert oc.from_vendor_response(_body("stop", message), model="m", latency_ms=1).text == "ab"


def test_content_filter_becomes_content_filtered_error():
    with pytest.raises(ContentFilteredError) as info:
        oc.from_vendor_response(_body("content_filter"), model="m", latency_ms=1)
    assert info.value.completion.usage.input_tokens == 10


def test_no_choices_is_terminal():
    with pytest.raises(TerminalError):
        oc.from_vendor_response({"choices": []}, model="m", latency_ms=1)


def _provider(handler, **kw):
    return oc.OpenAICompatProvider(
        base_url="http://gateway.invalid/v1",
        transport=httpx.MockTransport(handler),
        timeout_s=kw.pop("timeout_s", 5.0),
        **kw,
    )


async def test_http_errors_are_classified():
    outcomes = iter(
        [
            (429, {"retry-after": "7"}, {"error": {"message": "slow down"}}),
            (400, {}, {"error": {"message": "bad request"}}),
            (404, {}, {"error": {"message": "model 'nope' not found"}}),
            (503, {}, {"error": "down"}),
            (401, {}, "not json"),
        ]
    )

    def handler(request):
        status, headers, body = next(outcomes)
        if isinstance(body, str):
            return httpx.Response(status, headers=headers, text=body)
        return httpx.Response(status, headers=headers, json=body)

    p = _provider(handler)
    msgs = [Message.user("q")]

    with pytest.raises(RetryableError) as info:
        await p.complete(msgs, model="m")
    assert info.value.status == 429 and info.value.retry_after_s == 7.0

    with pytest.raises(TerminalError) as info:
        await p.complete(msgs, model="m")
    assert info.value.status == 400 and "bad request" in str(info.value)

    with pytest.raises(TerminalError) as info:
        await p.complete(msgs, model="nope")
    assert info.value.status == 404 and "not found" in str(info.value)

    with pytest.raises(RetryableError) as info:
        await p.complete(msgs, model="m")
    assert info.value.status == 503 and "down" in str(info.value)

    with pytest.raises(TerminalError) as info:
        await p.complete(msgs, model="m")
    assert info.value.status == 401 and "not json" in str(info.value)


async def test_timeouts_and_connection_errors_are_retryable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RequestTimeout) as info:
        await _provider(timeout, timeout_s=3.0).complete([Message.user("q")], model="m")
    assert info.value.timeout_s == 3.0

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RetryableError) as info:
        await _provider(refused).complete([Message.user("q")], model="m")
    assert not isinstance(info.value, RequestTimeout)


async def test_bearer_and_model_name_reach_the_wire():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_body(), headers={"x-request-id": "gw-9"})

    p = _provider(handler, api_key="sk-tenant-a", name="gateway")
    c = await p.complete([Message.user("q")], model="gpt-oss-120b", system="s", max_tokens=8)
    assert seen[0].url == httpx.URL("http://gateway.invalid/v1/chat/completions")
    assert seen[0].headers["authorization"] == "Bearer sk-tenant-a"
    sent = json.loads(seen[0].content)
    assert sent["model"] == "gpt-oss-120b" and sent["max_tokens"] == 8
    assert sent["messages"][0] == {"role": "system", "content": "s"}
    assert c.text == "ready" and p.name == "gateway"


async def test_no_key_means_no_authorization_header():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_body())

    await _provider(handler).complete([Message.user("q")], model="gpt-oss:20b")
    assert "authorization" not in seen[0].headers


async def test_token_source_is_awaited_per_call():
    tokens = iter(["tok-1", "tok-2"])
    seen = []

    async def source():
        return next(tokens)

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=_body())

    p = _provider(handler, token_source=source)
    await p.complete([Message.user("q")], model="m")
    await p.complete([Message.user("q")], model="m")
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


def test_google_endpoint_forms():
    assert oc.google_maas_url("proj", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/proj/locations/us-central1"
        "/endpoints/openapi"
    )
    assert oc.google_maas_url("proj", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/endpoints/openapi"
    )
    p = oc.for_google_maas("proj", "us-central1", token_source=lambda: None)
    assert p.name == "google-maas" and p.endpoint.endswith("/endpoints/openapi")
    assert oc.for_ollama("http://localhost:11434/v1/").endpoint == "http://localhost:11434/v1"
