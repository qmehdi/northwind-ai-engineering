"""Open-weight and Amazon models on Bedrock, through the Converse API.

The Anthropic SDK only speaks the Messages API, and gpt-oss and Nova do not. The
Converse API is Bedrock's one request shape for every model it hosts, so this
provider translates the course's types into Converse blocks and back, the same
way `anthropic_base.py` does for Claude. It is the only module that knows the
Converse shapes, and the only place botocore exceptions are read.

Model ids, fetched 2026-09-29 from the Bedrock model cards
(docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html):

- gpt-oss-120b: `openai.gpt-oss-120b-1:0` (bedrock-runtime, Converse and tool use). No
  `us.` or `global.` inference profile exists; the only geo profile is GovCloud's
  `us-gov.openai.gpt-oss-120b-1:0`. In-region in us-east-1, us-east-2, us-west-2 and the
  EU and APAC regions on the card.
- gpt-oss-20b: `openai.gpt-oss-20b-1:0`, same shape and regions.
- Nova Micro: `amazon.nova-micro-v1:0`; geo profiles `us.amazon.nova-micro-v1:0` and
  `eu.amazon.nova-micro-v1:0`. The `us.` profile serves us-east-1, us-east-2 and us-west-2.

A cross-region inference profile id (`us.`, `eu.`, `apac.`, `global.`, `us-gov.` before
the model id) is passed to `modelId` unchanged: Converse accepts a model id, a profile id
or either one's ARN in the same field.

Auth is the AWS credential chain (profile, environment, or the execution role in the
cloud). boto3 is synchronous, so the call runs in a worker thread; the SDK's own retries
are off because the course's retry loop is the one in charge.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from nw.llm.errors import ContentFilteredError, RequestTimeout, RetryableError, TerminalError
from nw.llm.types import Completion, Message, StopReason, ToolCall, ToolSpec, Usage

# Converse stop reasons that mean the provider blocked the content.
_REFUSALS = {"guardrail_intervened", "content_filtered"}

_STOP = {
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "guardrail_intervened": StopReason.REFUSAL,
    "content_filtered": StopReason.REFUSAL,
}

# botocore error codes, by family. Anything with a 5xx status is retryable too.
_RETRYABLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
    "ModelNotReadyException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelErrorException",
}
_TIMEOUT_CODES = {"ModelTimeoutException"}
_TIMEOUT_CLASSES = {"ReadTimeoutError", "ConnectTimeoutError"}
_CONNECTION_CLASSES = {"EndpointConnectionError", "ConnectionClosedError", "ResponseStreamingError"}


def to_vendor_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user" and m.tool_results:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": r.tool_call_id,
                                "content": [{"text": r.content}],
                                "status": "error" if r.is_error else "success",
                            }
                        }
                        for r in m.tool_results
                    ],
                }
            )
        elif m.role == "assistant" and m.raw_content:
            # the model's own blocks, reasoningContent included, exactly as it produced them
            out.append({"role": "assistant", "content": m.raw_content})
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"text": m.content})
            blocks.extend(
                {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}}
                for c in m.tool_calls
            )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": [{"text": m.content or ""}]})
    return out


def to_vendor_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "toolSpec": {
                "name": t.name,
                "description": t.description,
                "inputSchema": {"json": t.input_schema},
            }
        }
        for t in tools
    ]


def build_request(
    messages: list[Message],
    *,
    model: str,
    system: str | None,
    tools: list[ToolSpec] | None,
    max_tokens: int,
    temperature: float | None,
) -> dict[str, Any]:
    """The Converse request. Pure, so the translation is testable without a network."""
    inference: dict[str, Any] = {"maxTokens": max_tokens}
    if temperature is not None:
        inference["temperature"] = temperature
    kwargs: dict[str, Any] = {
        "modelId": model,
        "messages": to_vendor_messages(messages),
        "inferenceConfig": inference,
    }
    if system:
        kwargs["system"] = [{"text": system}]
    vendor_tools = to_vendor_tools(tools)
    if vendor_tools:
        kwargs["toolConfig"] = {"tools": vendor_tools}
    return kwargs


def from_vendor_response(response: dict[str, Any], *, model: str, latency_ms: float) -> Completion:
    content = response.get("output", {}).get("message", {}).get("content", []) or []
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            use = block["toolUse"]
            tool_calls.append(
                ToolCall(
                    id=use["toolUseId"], name=use["name"], arguments=dict(use.get("input") or {})
                )
            )
    usage = response.get("usage", {}) or {}
    stop_raw = response.get("stopReason") or ""
    stop = _STOP.get(stop_raw, StopReason.OTHER)
    request_id = (response.get("ResponseMetadata") or {}).get("RequestId") or ""
    completion = Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.get("inputTokens", 0) or 0,
            output_tokens=usage.get("outputTokens", 0) or 0,
            cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
            cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
            latency_ms=latency_ms,
        ),
        request_id=request_id,
        model=model,
        stop_reason=stop,
        raw_content=[dict(b) for b in content],
    )
    if stop_raw in _REFUSALS:
        err = ContentFilteredError(
            f"provider refused the request (stop_reason={stop_raw})", request_id=request_id
        )
        err.completion = completion  # the refusal still cost tokens; the client meters it
        raise err
    return completion


def classify(exc: Exception, *, timeout_s: float) -> RetryableError | TerminalError:
    """Map a botocore exception onto the course's two error families.

    Read structurally (the `response` dict of a `ClientError`, the class name of a
    connection error) so this module never imports botocore and the tests need no AWS
    package to exercise every branch.
    """
    name = type(exc).__name__
    if name in _TIMEOUT_CLASSES:
        return RequestTimeout(f"request timed out ({exc})", timeout_s=timeout_s)
    if name in _CONNECTION_CLASSES:
        return RetryableError(str(exc))
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return TerminalError(str(exc))
    error = response.get("Error") or {}
    meta = response.get("ResponseMetadata") or {}
    code = error.get("Code") or name
    status = meta.get("HTTPStatusCode")
    request_id = meta.get("RequestId")
    message = f"{code}: {error.get('Message') or exc}"
    if code in _TIMEOUT_CODES:
        return RequestTimeout(message, timeout_s=timeout_s, request_id=request_id)
    if code in _RETRYABLE_CODES or (isinstance(status, int) and status >= 500):
        return RetryableError(
            message, request_id=request_id, retry_after_s=_retry_after(meta), status=status
        )
    return TerminalError(message, request_id=request_id, status=status)


def _retry_after(meta: dict[str, Any]) -> float | None:
    headers = meta.get("HTTPHeaders") or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class BedrockConverseProvider:
    name = "bedrock-converse"

    def __init__(
        self,
        *,
        region: str,
        profile: str | None = None,
        timeout_s: float = 60.0,
        client: Any | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self.endpoint = f"https://bedrock-runtime.{region}.amazonaws.com"
        self._client = client if client is not None else _make_client(region, profile, timeout_s)

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> Completion:
        kwargs = build_request(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        started = time.perf_counter()
        try:
            response = await asyncio.to_thread(self._client.converse, **kwargs)
        except Exception as exc:  # noqa: BLE001  (every botocore family is classified)
            raise classify(exc, timeout_s=self._timeout_s) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return from_vendor_response(response, model=model, latency_ms=latency_ms)


def _make_client(region: str, profile: str | None, timeout_s: float) -> Any:
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile, region_name=region)
    # standard mode, one attempt: the course's own retry loop is the one in charge, so its
    # behaviour is visible and testable. The SDK would otherwise retry silently.
    config = Config(
        read_timeout=timeout_s,
        connect_timeout=min(10.0, timeout_s),
        retries={"mode": "standard", "max_attempts": 1},
    )
    return session.client("bedrock-runtime", config=config)
