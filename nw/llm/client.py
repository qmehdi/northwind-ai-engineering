"""LLMClient: the one object every later project talks to a model through.

It wraps any provider with the four things a production caller needs and a
raw SDK does not give you in one place:

1. Bounded concurrency: a semaphore, so a fan-out of 500 prompts never opens
   500 connections and never trips the provider's rate limit by itself.
2. Retries that respect the provider: retryable and terminal failures are
   different, Retry-After is honoured, backoff has jitter.
3. Structured output with one repair round trip, validated by Pydantic.
   The model's JSON is never trusted; the schema is.
4. Cost metering with a hard cap, and a correlation ID on every call.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from nw.config import ModelRole, Settings
from nw.config import settings as default_settings
from nw.llm.cost import CostMeter
from nw.llm.provider import LLMProvider
from nw.llm.retry import RetryPolicy
from nw.llm.types import Completion, Message, ToolSpec, Usage
from nw.logging import get_logger

Sleep = Callable[[float], Awaitable[None]]

log = get_logger("nw.llm.client")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        settings: Settings | None = None,
        max_concurrency: int | None = None,
        retry: RetryPolicy | None = None,
        meter: CostMeter | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings or default_settings()
        self._provider = provider
        self._sem = asyncio.Semaphore(max_concurrency or self._settings.max_concurrency)
        self._retry = retry or RetryPolicy()
        self.meter = meter or CostMeter(cap_usd=self._settings.spend_cap_usd)
        self._sleep = sleep

    # ----- public API -------------------------------------------------------

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    @property
    def spend_usd(self) -> float:
        return self.meter.total_usd

    @property
    def usage(self) -> Usage:
        return self.meter.total_usage

    def model_for(self, role: ModelRole) -> str:
        return self._settings.model_for(role)

    async def complete(
        self,
        prompt: str | list[Message],
        *,
        role: ModelRole = ModelRole.WORKHORSE,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> Completion:
        """One completion, with concurrency limiting, retries, and metering.

        Order of operations matters:
        1. Reserve budget on the meter (raises SpendCapExceeded before any network).
        2. Take the semaphore, then call the provider under `_with_retries`.
        3. Release the reservation, record the real cost, log one line.
        """
        raise NotImplementedError("Session 1, Step 2: semaphore, retries, metering")

    async def structured[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        role: ModelRole = ModelRole.WORKHORSE,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> T:
        """Return a validated instance of `schema`.

        The model is told the JSON Schema and asked for JSON only. If the reply does
        not parse or does not validate, one repair round trip sends the error back.
        After that, StructuredOutputError. Never loop forever on a model that will
        not comply; that is a budget leak.
        """
        raise NotImplementedError("Session 1, Step 4: schema prompt, parse, one repair")

    async def map(
        self,
        prompts: list[str],
        *,
        role: ModelRole = ModelRole.WORKHORSE,
        system: str | None = None,
        max_tokens: int = 1024,
        return_exceptions: bool = False,
    ) -> list[Completion | BaseException]:
        """Fan out over `prompts` with bounded concurrency. Results come back in
        input order regardless of the order in which the provider answered."""
        raise NotImplementedError("Session 1, Step 5: gather, keep input order")

    # ----- internals --------------------------------------------------------

    async def _with_retries(
        self, request_id: str, call: Callable[[], Awaitable[Completion]]
    ) -> Completion:
        """Run `call` under the retry policy. Terminal errors are raised at once."""
        raise NotImplementedError("Session 1, Step 3: classify, back off, give up")


def _parse_as[T: BaseModel](text: str, schema: type[T]) -> T:
    cleaned = _FENCE.sub("", text.strip()).strip()
    # Tolerate prose around the object by taking the outermost braces.
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in reply")
        cleaned = cleaned[start : end + 1]
    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg} at position {exc.pos}") from exc
    return schema.model_validate(data)


def _short(s: str, limit: int = 600) -> str:
    return s if len(s) <= limit else s[:limit] + "..."
