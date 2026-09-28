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

Two more, added when the services went behind a load balancer:

5. A per-call timeout (`Settings.request_timeout_s`) enforced here as well as in the
   vendor client, surfaced as `RequestTimeout`, which is retryable.
6. A fallback model per role and a circuit breaker per model. When the primary fails
   with a terminal 404, a 400 that names the model, or exhausts its retries, the fallback
   is tried once; the span and the cost record say `fallback=true`. A model that keeps
   failing has its circuit opened and is skipped without paying the retries every call.
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
from nw.llm.breaker import CircuitBreaker
from nw.llm.cost import CostMeter
from nw.llm.errors import (
    CircuitOpenError,
    LLMError,
    RequestTimeout,
    RetryableError,
    StructuredOutputError,
    TerminalError,
    TokenBudgetExceeded,
)
from nw.llm.provider import LLMProvider
from nw.llm.retry import RetryPolicy
from nw.llm.types import Completion, Message, ToolSpec, Usage
from nw.logging import get_logger, log_fields

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
        breaker: CircuitBreaker | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._settings = settings or default_settings()
        self._provider = provider
        self._sem = asyncio.Semaphore(max_concurrency or self._settings.max_concurrency)
        self._retry = retry or RetryPolicy()
        self.meter = meter or CostMeter(cap_usd=self._settings.spend_cap_usd)
        self._sleep = sleep
        self.breaker = breaker or CircuitBreaker(
            threshold=self._settings.breaker_failures, open_s=self._settings.breaker_open_s
        )
        self._timeout_s = timeout_s or self._settings.request_timeout_s

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

    def fallback_for(self, role: ModelRole) -> str | None:
        return self._settings.fallback_for(role)

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    async def complete(
        self,
        prompt: str | list[Message],
        *,
        role: ModelRole = ModelRole.WORKHORSE,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        prompt_version: str | None = None,
        timeout_s: float | None = None,
    ) -> Completion:
        """One completion, with concurrency limiting, retries, and metering.

        Order of operations matters:
        1. Take the semaphore. Only calls that are about to go on the wire hold budget;
           a thousand queued calls must not reserve a thousand times the estimate.
        2. Reserve budget on the meter (raises SpendCapExceeded before any network).
        3. Call the provider under `_with_retries`, on the primary model, then once on the
           role's fallback when the primary fails the way a fallback can help with. Each
           attempt runs under the per-call timeout and is skipped when its circuit is open.
        4. Release the reservation, record the real cost, log one line.

        `prompt_version` names the system prompt on the span (`nw.prompt_version`); when the
        caller does not pass one it is looked up from the registry by the text of `system`.
        `timeout_s` overrides `Settings.request_timeout_s` for this call.
        """
        raise NotImplementedError("Service layer, Step 4: semaphore, retries, metering")

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

        The span names the caller's prompt, not the wrapped one: the schema instructions are
        a registered prompt of their own (`llm.structured`) and their hash is in every report.
        """
        raise NotImplementedError("Service layer, Step 5: schema prompt, parse, one repair")

    async def map(
        self,
        prompts: list[str],
        *,
        role: ModelRole = ModelRole.WORKHORSE,
        system: str | None = None,
        max_tokens: int = 1024,
        return_exceptions: bool = False,
        max_total_tokens: int | None = None,
    ) -> list[Completion | BaseException]:
        """Fan out over `prompts` with bounded concurrency. Results come back in
        input order regardless of the order in which the provider answered.

        `max_total_tokens` is a budget for the whole fan-out, input plus output. Once the
        completions so far have used it up, the prompts still waiting fail with
        `TokenBudgetExceeded` instead of being sent; calls already in flight finish, so the
        overshoot is at most one call per concurrency slot."""
        raise NotImplementedError("Service layer, Step 6: gather, keep input order")

    # ----- internals --------------------------------------------------------

    async def _timed(self, call: Awaitable[Completion], limit_s: float, model: str) -> Completion:
        """The per-call timeout, enforced here whatever the provider does. The vendor
        clients carry the same value, so on the wire the SDK's timeout fires first and
        arrives already classified; this catches a provider that does not have one."""
        try:
            return await asyncio.wait_for(call, timeout=limit_s)
        except TimeoutError as exc:
            raise RequestTimeout(
                f"{model} did not answer within {limit_s:g}s", timeout_s=limit_s
            ) from exc

    async def _with_fallback(
        self,
        request_id: str,
        role: ModelRole,
        call: Callable[[str], Awaitable[Completion]],
    ) -> tuple[Completion, _Outcome]:
        """Primary first, then the role's fallback once. The breaker is consulted before
        each candidate and told the result after; an error that a fallback cannot help
        with (a refusal, a bad request that is ours) is raised without trying it."""
        primary = self.model_for(role)
        fallback = self.fallback_for(role)
        candidates = [primary] + ([fallback] if fallback else [])
        skipped: list[str] = []
        last: LLMError | None = None
        attempts = 0  # provider round trips over every candidate, retries included
        for i, model in enumerate(candidates):
            if not self.breaker.allow(model):
                skipped.append(model)
                log.warning(
                    "circuit open, model skipped",
                    extra=log_fields(request_id=request_id, model=model, role=role.value),
                )
                continue

            def attempt(m: str = model) -> Awaitable[Completion]:
                nonlocal attempts
                attempts += 1
                return call(m)

            try:
                completion = await self._with_retries(request_id, attempt)
            except LLMError as exc:
                if _counts_as_model_failure(exc):
                    self.breaker.failure(model)
                last = exc
                if i + 1 < len(candidates) and _fallback_worthy(exc):
                    log.warning(
                        "falling back",
                        extra=log_fields(
                            request_id=request_id,
                            model=model,
                            fallback=candidates[i + 1],
                            error=str(exc),
                            status=getattr(exc, "status", None),
                        ),
                    )
                    continue
                raise
            self.breaker.success(model)
            return completion, _Outcome(model, i > 0, attempts, tuple(skipped))
        if last is not None:
            raise last
        raise CircuitOpenError(
            f"no model available for role {role.value}: circuit open for {', '.join(skipped)}",
            models=skipped,
        )

    async def _with_retries(
        self, request_id: str, call: Callable[[], Awaitable[Completion]]
    ) -> Completion:
        """Run `call` under the retry policy. Terminal errors are raised at once."""
        raise NotImplementedError("Service layer, Step 4: classify, back off, give up")


class _Outcome:
    """How a completion was obtained: which model, whether it was the fallback, how many
    provider round trips, and which models the breaker skipped."""

    __slots__ = ("attempts", "fallback", "model", "skipped")

    def __init__(self, model: str, fallback: bool, attempts: int, skipped: tuple[str, ...]):
        self.model, self.fallback, self.attempts, self.skipped = model, fallback, attempts, skipped


class _TokenBudget:
    """A shared counter for `map`: input plus output tokens over the fan-out so far."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0

    def check(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise TokenBudgetExceeded(
                f"token budget of {self.limit} used up ({self.used} tokens so far); "
                "the remaining prompts were not sent"
            )

    def add(self, usage: Usage) -> None:
        self.used += usage.input_tokens + usage.output_tokens


def _fallback_worthy(exc: LLMError) -> bool:
    """A fallback model can help with: retries exhausted (429, 529, 5xx, timeouts); a 404
    (the model id is wrong or not in this region); a 400 that names the model (not
    available, not enabled). Not with a refusal, an auth error or a bad request of ours."""
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, TerminalError) and not isinstance(exc, StructuredOutputError):
        if exc.status == 404:
            return True
        if exc.status == 400 and "model" in str(exc).lower():
            return True
    return False


def _counts_as_model_failure(exc: LLMError) -> bool:
    """What the breaker counts: the same failures a fallback is for. Content refusals and
    our own bad requests are not the model being down."""
    return _fallback_worthy(exc)


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
