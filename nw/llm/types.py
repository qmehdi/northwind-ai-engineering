"""The course's own types for talking to a model.

Every provider translates its vendor's shapes into these and back. Nothing
outside `nw/llm/providers/` sees a vendor SDK object.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
        )


class ToolSpec(BaseModel):
    """What a model is told about a tool. `input_schema` is JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    content: str
    is_error: bool = False


class Message(BaseModel):
    """One turn. A user turn carries text or tool results; an assistant turn
    carries text and any tool calls it made."""

    role: Literal["user", "assistant"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    # The vendor's own content blocks for an assistant turn (text, tool_use, thinking).
    # Replayed verbatim so a model that thinks keeps its thinking blocks in a tool loop.
    raw_content: list[dict[str, Any]] | None = None

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=text)

    @classmethod
    def assistant(
        cls,
        text: str,
        tool_calls: list[ToolCall] | None = None,
        raw_content: list[dict[str, Any]] | None = None,
    ) -> Message:
        return cls(
            role="assistant", content=text, tool_calls=tool_calls or [], raw_content=raw_content
        )

    @classmethod
    def from_completion(cls, completion: Completion) -> Message:
        return cls.assistant(completion.text, completion.tool_calls, completion.raw_content)

    @classmethod
    def results(cls, results: list[ToolResult]) -> Message:
        return cls(role="user", tool_results=results)


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class Completion(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage
    request_id: str
    model: str
    stop_reason: StopReason = StopReason.END_TURN
    raw_content: list[dict[str, Any]] | None = None
    # What this completion cost, set by `LLMClient.complete` (the provider leaves it None).
    # Excluded from dumps: a trajectory or a cache entry stores the answer, not the bill.
    cost: Any = Field(default=None, exclude=True, repr=False)

    @property
    def cost_usd(self) -> float:
        """This call's cost in USD, 0.0 when the client has not priced it."""
        return float(getattr(self.cost, "cost_usd", 0.0) or 0.0)
