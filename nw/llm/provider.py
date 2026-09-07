"""The provider interface. One implementation per vendor endpoint."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nw.llm.types import Completion, Message, ToolSpec


@runtime_checkable
class LLMProvider(Protocol):
    name: str

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
        """One round trip. Raises RetryableError or TerminalError, nothing else."""
        ...
