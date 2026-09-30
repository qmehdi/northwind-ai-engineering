"""Shared translation between the course's types and the Anthropic SDK.

Bedrock and Vertex differ only in how the client is constructed; the request
and response shapes are the Messages API on both. This module owns the
translation and the error classification. It is the only module that imports
`anthropic` types.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from nw.llm.errors import (
    RETRYABLE_STATUS,
    ContentFilteredError,
    RequestTimeout,
    RetryableError,
    TerminalError,
)
from nw.llm.types import Completion, Message, StopReason, ToolCall, ToolSpec, Usage

# Models that reject sampling parameters. Sending temperature to these is a 400.
_NO_SAMPLING_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-fable", "claude-opus-4-")


def rejects_sampling(model: str) -> bool:
    bare = model.split(".")[-1]  # strips `anthropic.`, `global.anthropic.`, `us.anthropic.`
    return bare.startswith(_NO_SAMPLING_PREFIXES)


def to_vendor_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user" and m.tool_results:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.tool_call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in m.tool_results
                    ],
                }
            )
        elif m.role == "assistant" and m.raw_content:
            # the model's own blocks, thinking included, exactly as it produced them
            out.append({"role": "assistant", "content": m.raw_content})
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


CACHE = {"type": "ephemeral"}


def to_vendor_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    """The tool list is the same on every step of a loop, so its last entry carries a cache
    breakpoint: everything up to and including it is served from the prompt cache."""
    if not tools:
        return None
    out = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]
    out[-1] = {**out[-1], "cache_control": CACHE}
    return out


def build_request(
    messages: list[Message],
    *,
    model: str,
    system: str | None,
    tools: list[ToolSpec] | None,
    max_tokens: int,
    temperature: float | None,
) -> dict[str, Any]:
    """The Messages API request. Pure, so the translation is testable without a network."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": to_vendor_messages(messages),
    }
    if system:
        kwargs["system"] = [{"type": "text", "text": system, "cache_control": CACHE}]
    vendor_tools = to_vendor_tools(tools)
    if vendor_tools:
        kwargs["tools"] = vendor_tools
    if temperature is not None and not rejects_sampling(model):
        kwargs["temperature"] = temperature
    return kwargs


def _raw_blocks(response: Any) -> list[dict[str, Any]]:
    out = []
    for block in response.content:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            out.append(dict(block))
    return out


def from_vendor_response(response: Any, *, latency_ms: float) -> Completion:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
            )
    usage = response.usage
    stop = {
        "end_turn": StopReason.END_TURN,
        "tool_use": StopReason.TOOL_USE,
        "max_tokens": StopReason.MAX_TOKENS,
        "refusal": StopReason.REFUSAL,
    }.get(response.stop_reason or "", StopReason.OTHER)
    completion = Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=latency_ms,
        ),
        request_id=response.id,
        model=response.model,
        stop_reason=stop,
        raw_content=_raw_blocks(response),
    )
    if stop is StopReason.REFUSAL:
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        err = ContentFilteredError(
            f"provider refused the request (category={category})", request_id=response.id
        )
        err.completion = completion  # the refusal still cost tokens; the client meters it
        raise err
    return completion


def classify(exc: Exception) -> RetryableError | TerminalError:
    """Map an SDK exception onto the course's two error families."""
    request_id = getattr(exc, "request_id", None)
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = _retry_after(exc)
        return RetryableError(
            "rate limited", request_id=request_id, retry_after_s=retry_after, status=429
        )
    if isinstance(exc, anthropic.APITimeoutError):
        # The SDK client was built with `timeout=request_timeout_s`; this is that timeout.
        timeout = getattr(getattr(exc, "request", None), "extensions", {}).get("timeout")
        seconds = _timeout_seconds(timeout)
        return RequestTimeout(
            f"request timed out ({exc})", timeout_s=seconds, request_id=request_id
        )
    if isinstance(
        exc,
        anthropic.OverloadedError
        | anthropic.InternalServerError
        | anthropic.ServiceUnavailableError
        | anthropic.APIConnectionError,
    ):
        status = getattr(exc, "status_code", None)
        return RetryableError(str(exc), request_id=request_id, status=status)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in RETRYABLE_STATUS:
            return RetryableError(
                str(exc),
                request_id=request_id,
                retry_after_s=_retry_after(exc),
                status=exc.status_code,
            )
        return TerminalError(
            str(exc), request_id=request_id, status=exc.status_code, code=_error_type(exc)
        )
    return TerminalError(str(exc), request_id=request_id)


def _error_type(exc: anthropic.APIStatusError) -> str | None:
    """The Messages API error type (`not_found_error`, `invalid_request_error`) from the body:
    `{"type": "error", "error": {"type": ...}}`."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            return error["type"]
        if isinstance(body.get("type"), str) and body["type"] != "error":
            return body["type"]
    return None


def _timeout_seconds(timeout: Any) -> float:
    """httpx stores the timeout on the request as a dict of phases; take the read phase."""
    if isinstance(timeout, dict):
        value = timeout.get("read") or timeout.get("pool")
        return float(value) if value else 0.0
    return float(timeout) if isinstance(timeout, int | float) else 0.0


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class AnthropicMessagesProvider:
    """Base for the Bedrock and Vertex providers. Subclasses build `self._client`."""

    name = "anthropic"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def aclose(self) -> None:
        """Close the SDK's HTTP client (the connection pool); safe to call twice."""
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

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
            response = await self._client.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            raise classify(exc) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return from_vendor_response(response, latency_ms=latency_ms)
