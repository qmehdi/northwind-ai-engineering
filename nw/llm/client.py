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
   because the model is unavailable (a 404 or a vendor code such as `model_not_found`,
   `ResourceNotFoundException`, `DeploymentNotFound`) or exhausts its retries, the fallback
   is tried once; the span and the cost record say `fallback=true`. A model that keeps
   failing has its circuit opened and is skipped without paying the retries every call.

And three that make it hold under load:

7. A concurrency slot is held only while a request is on the wire: a retry's back-off sleeps
   without one, so a struggling provider cannot starve every other caller of slots.
8. An overall deadline per call (`Settings.call_deadline_s`) over every attempt, back-off and
   the fallback, so the worst case is a number you chose.
9. Cost attributed to the caller: every `Completion` carries its `cost` record, and
   `cost_scope()` collects the records of one run however many runs share the client.
   Refusals that consumed tokens are metered too. Calls for an EU account (`nw.llm.residency`)
   go to the role's EU-resident model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ValidationError

from nw.config import ModelRole, Residency, Settings
from nw.config import settings as default_settings
from nw.llm.breaker import CircuitBreaker
from nw.llm.cost import CostMeter, CostScope, cost_scope, cost_usd
from nw.llm.errors import (
    CircuitOpenError,
    ContentFilteredError,
    LLMError,
    RequestTimeout,
    ResidencyError,
    RetryableError,
    StructuredOutputError,
    TerminalError,
    TokenBudgetExceeded,
)
from nw.llm.prompts import STRUCTURED_INSTRUCTIONS, version_for
from nw.llm.provider import LLMProvider
from nw.llm.residency import current_residency
from nw.llm.retry import RetryPolicy
from nw.llm.types import Completion, Message, ToolSpec, Usage
from nw.logging import correlation_id, get_logger, log_fields
from nw.telemetry import span

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
        deadline_s: float | None = None,
    ) -> None:
        self._settings = settings or default_settings()
        self._provider = provider
        self._max_concurrency = max_concurrency or self._settings.max_concurrency
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._retry = retry or RetryPolicy()
        self.meter = meter or CostMeter(cap_usd=self._settings.spend_cap_usd)
        self._sleep = sleep
        self.breaker = breaker or CircuitBreaker(
            threshold=self._settings.breaker_failures, open_s=self._settings.breaker_open_s
        )
        self._timeout_s = timeout_s or self._settings.request_timeout_s
        self._deadline_s = deadline_s or self._settings.call_deadline_s

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
        """The role's model for the current residency (`nw.llm.residency.bind_residency`)."""
        return self._settings.model_for(role, current_residency())

    def fallback_for(self, role: ModelRole) -> str | None:
        return self._settings.fallback_for(role, current_residency())

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def deadline_s(self) -> float:
        return self._deadline_s

    @contextmanager
    def cost_scope(self) -> Iterator[CostScope]:
        """Collect the cost records of every call made in this block, and only those:

            with client.cost_scope() as run:
                await run_agent(...)
            run.total_usd, run.tokens

        Concurrent runs on one client each get their own scope (a context variable), so a
        per-run budget or `cost_usd` never includes another run's calls."""
        with cost_scope() as scope:
            yield scope

    async def aclose(self) -> None:
        """Close the provider's HTTP clients and worker threads. Call it on shutdown."""
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()

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
        """One completion, with concurrency limiting, retries, and metering. The returned
        Completion carries its `cost` record (`completion.cost_usd`).

        Order of operations matters:
        1. Resolve the model for the role and the current residency (an EU account gets the
           EU-resident id, or `ResidencyError` before any network).
        2. Each attempt takes the semaphore and reserves budget on the meter (raises
           SpendCapExceeded before any network), and gives both back when the attempt ends.
           Only calls on the wire hold a slot or budget; a back-off sleeps without either.
        3. Call the provider under `_with_retries`, on the primary model, then once on the
           role's fallback when the primary fails the way a fallback can help with. Each
           attempt runs under the per-call timeout, cut to what is left of the call's
           deadline, and is skipped when its circuit is open.
        4. Record the real cost (a refusal's too), log one line.

        `prompt_version` names the system prompt on the span (`nw.prompt_version`); when the
        caller does not pass one it is looked up from the registry by the text of `system`.
        `timeout_s` overrides `Settings.request_timeout_s` for this call.
        """
        # SOLUTION BEGIN
        messages = [Message.user(prompt)] if isinstance(prompt, str) else prompt
        residency = current_residency()
        try:
            model = self.model_for(role)
        except ResidencyError as exc:
            # Refused before any network, and said so: never silently sent out of the zone.
            log.warning(
                "residency refused",
                extra=log_fields(role=role.value, residency=residency.value, reason=str(exc)),
            )
            raise
        request_id = uuid.uuid4().hex[:12]
        # A rough reservation: assume the call costs as much as the largest output we
        # allowed plus a modest prompt. It is released and replaced by the real cost.
        estimate = cost_usd(model, Usage(input_tokens=2 * max_tokens, output_tokens=max_tokens))
        prompt_version = prompt_version or version_for(system)
        timeout = timeout_s or self._timeout_s
        deadline = _Deadline(self._deadline_s)

        async def attempt(m: str) -> Completion:
            async with self._sem:
                self.meter.reserve(estimate)
                try:
                    return await self._timed(
                        self._provider.complete(
                            messages,
                            model=m,
                            system=system,
                            tools=tools,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        ),
                        deadline.cap(timeout, m),
                        m,
                    )
                finally:
                    self.meter.release(estimate)

        with span(
            "llm.complete",
            **{
                "gen_ai.request.model": model,
                "nw.role": role.value,
                "nw.prompt_version": prompt_version,
                "nw.residency": residency.value,
            },
        ) as sp:
            try:
                completion, outcome = await self._with_fallback(
                    request_id, role, attempt, deadline=deadline
                )
            except ContentFilteredError as exc:
                # The refusal still consumed tokens: meter it, and hand the record to the caller.
                refused = exc.completion
                if refused is not None:
                    exc.cost = self.meter.record(
                        request_id,
                        refused.model or model,
                        refused.usage,
                        correlation_id(),
                        role=role.value,
                        refused=True,
                        residency=residency.value,
                    )
                    sp.set_attribute("nw.cost_usd", exc.cost.cost_usd)
                raise
            model = outcome.model
            sp.set_attribute("gen_ai.request.model", model)
            sp.set_attribute("gen_ai.usage.input_tokens", completion.usage.input_tokens)
            sp.set_attribute("gen_ai.usage.output_tokens", completion.usage.output_tokens)
            sp.set_attribute("nw.stop_reason", completion.stop_reason.value)
            sp.set_attribute("nw.fallback", outcome.fallback)
            sp.set_attribute("nw.attempts", outcome.attempts)
            if outcome.skipped:
                sp.set_attribute("nw.breaker_skipped", ",".join(outcome.skipped))
            record = self.meter.record(
                request_id,
                model,
                completion.usage,
                correlation_id(),
                role=role.value,
                fallback=outcome.fallback,
                attempts=outcome.attempts,
                skipped=outcome.skipped,
                residency=residency.value,
            )
            sp.set_attribute("nw.cost_usd", record.cost_usd)
        log.info(
            "completion",
            extra=log_fields(
                request_id=request_id,
                provider_request_id=completion.request_id,
                model=model,
                role=role.value,
                residency=residency.value,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                latency_ms=round(completion.usage.latency_ms, 1),
                cost_usd=round(record.cost_usd, 6),
                spend_usd=round(self.meter.total_usd, 6),
                stop_reason=completion.stop_reason.value,
                prompt_version=prompt_version,
                fallback=outcome.fallback,
                attempts=outcome.attempts,
            ),
        )
        return completion.model_copy(update={"cost": record})
        # STUB: raise NotImplementedError("Service layer, complete: semaphore, retries, metering")
        # SOLUTION END

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
        The schema is part of the version (`<prompt>+schema.<Name>@<hash>`): a field added to
        the schema changes what the model is asked for as surely as a prompt edit does.
        """
        # SOLUTION BEGIN
        schema_json = json.dumps(schema.model_json_schema(), indent=None)
        instructions = (
            f"{system.rstrip() if system else ''}\n\n{STRUCTURED_INSTRUCTIONS.text}\n{schema_json}"
        ).strip()
        prompt_version = schema_version(version_for(system), schema.__name__, schema_json)
        messages = [Message.user(prompt)]
        last_error: str | None = None
        for attempt in range(2):
            completion = await self.complete(
                messages,
                role=role,
                system=instructions,
                max_tokens=max_tokens,
                prompt_version=prompt_version,
            )
            try:
                return _parse_as(completion.text, schema)
            except (ValueError, ValidationError) as exc:
                last_error = _short(str(exc))
                log.warning(
                    "structured output invalid",
                    extra=log_fields(attempt=attempt + 1, error=last_error),
                )
                messages = messages + [
                    Message.assistant(completion.text),
                    Message.user(
                        "That was not valid. Error:\n"
                        f"{last_error}\n"
                        "Reply again with only the corrected JSON object."
                    ),
                ]
        raise StructuredOutputError(f"output did not match {schema.__name__}: {last_error}")
        # STUB: raise NotImplementedError("Structured output: schema prompt, parse, repair")
        # SOLUTION END

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
        overshoot is at most one call per concurrency slot. The budget is checked after a
        prompt has its turn (a slot of the fan-out), not when the task is created: checked
        at creation, every prompt would pass at t=0 against a provider that really waits."""
        # SOLUTION BEGIN
        budget = _TokenBudget(max_total_tokens)
        turns = asyncio.Semaphore(self._max_concurrency)

        async def one(p: str) -> Completion:
            async with turns:
                budget.check()
                c = await self.complete(p, role=role, system=system, max_tokens=max_tokens)
                budget.add(c.usage)
                return c

        tasks = [one(p) for p in prompts]
        # gather preserves order; as_completed would not.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if not return_exceptions:
            for r in results:
                if isinstance(r, BaseException):
                    raise r
        return list(results)
        # STUB: raise NotImplementedError("Service layer, fan out: gather, keep input order")
        # SOLUTION END

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
        *,
        deadline: _Deadline | None = None,
    ) -> tuple[Completion, _Outcome]:
        """Primary first, then the role's fallback once. The breaker is consulted before
        each candidate and told the result after; an error that a fallback cannot help
        with (a refusal, a bad request that is ours) is raised without trying it. The
        circuit of an EU-resident id is its own (`eu:<id>`): the same id in another region
        is another endpoint."""
        primary = self.model_for(role)
        fallback = self.fallback_for(role)
        candidates = [primary] + ([fallback] if fallback else [])
        zone = current_residency()
        skipped: list[str] = []
        last: LLMError | None = None
        attempts = 0  # provider round trips over every candidate, retries included
        for i, model in enumerate(candidates):
            circuit = model if zone is Residency.DEFAULT else f"{zone.value}:{model}"
            if deadline is not None and deadline.remaining() <= 0 and last is not None:
                break
            if not self.breaker.allow(circuit):
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
                completion = await self._with_retries(request_id, attempt, deadline=deadline)
            except LLMError as exc:
                if _counts_as_model_failure(exc):
                    self.breaker.failure(circuit)
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
            self.breaker.success(circuit)
            return completion, _Outcome(model, i > 0, attempts, tuple(skipped))
        if last is not None:
            raise last
        raise CircuitOpenError(
            f"no model available for role {role.value}: circuit open for {', '.join(skipped)}",
            models=skipped,
        )

    async def _with_retries(
        self,
        request_id: str,
        call: Callable[[], Awaitable[Completion]],
        *,
        deadline: _Deadline | None = None,
    ) -> Completion:
        """Run `call` under the retry policy. Terminal errors are raised at once. With a
        `deadline`, a back-off that would end past it is not taken: the last error is raised."""
        # SOLUTION BEGIN
        attempt = 0
        while True:
            attempt += 1
            try:
                return await call()
            except TerminalError:
                raise
            except RetryableError as exc:
                delay = self._retry.delay_for(attempt, exc.retry_after_s)
                out_of_time = deadline is not None and deadline.remaining() <= delay
                if attempt >= self._retry.max_attempts or out_of_time:
                    log.error(
                        "giving up",
                        extra=log_fields(
                            request_id=request_id,
                            attempts=attempt,
                            error=str(exc),
                            deadline=out_of_time,
                        ),
                    )
                    raise
                log.warning(
                    "retrying",
                    extra=log_fields(
                        request_id=request_id,
                        attempt=attempt,
                        delay_s=round(delay, 3),
                        status=exc.status,
                        retry_after_s=exc.retry_after_s,
                    ),
                )
                await self._sleep(delay)
                if deadline is not None:
                    deadline.slept(delay)
        # STUB: raise NotImplementedError("Service layer, complete: classify, back off, give up")
        # SOLUTION END


class _Deadline:
    """What is left of one call's time budget. Elapsed time is the larger of the wall clock and
    the back-off slept so far, so an injected sleep (the tests) counts like a real one."""

    def __init__(self, budget_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget_s = budget_s
        self._clock = clock
        self._start = clock()
        self._slept = 0.0

    def slept(self, seconds: float) -> None:
        self._slept += seconds

    def remaining(self) -> float:
        return self.budget_s - max(self._clock() - self._start, self._slept)

    def cap(self, timeout_s: float, model: str) -> float:
        """The attempt's timeout, cut to what is left; nothing left raises a timeout."""
        left = self.remaining()
        if left <= 0:
            raise RequestTimeout(
                f"{model}: call deadline of {self.budget_s:g}s used up", timeout_s=self.budget_s
            )
        return min(timeout_s, left)


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
    """A fallback model can help with: retries exhausted (429, 529, 5xx, timeouts); the model
    being unavailable, read from the status and the vendor's error code (`model_unavailable`:
    a 404, `not_found_error`, `ResourceNotFoundException`, `model_not_found`,
    `DeploymentNotFound`, `NOT_FOUND`). Not a refusal, an auth error, a residency refusal or a
    bad request of ours, whatever its message says."""
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, TerminalError) and not isinstance(
        exc, StructuredOutputError | ContentFilteredError | ResidencyError
    ):
        return exc.model_unavailable
    return False


def _counts_as_model_failure(exc: LLMError) -> bool:
    """What the breaker counts: the same failures a fallback is for. Content refusals and
    our own bad requests are not the model being down."""
    return _fallback_worthy(exc)


def schema_version(prompt_version: str | None, name: str, schema_json: str) -> str:
    """`<prompt version>+schema.<Name>@<12 hex>`: the prompt and the output contract together."""
    digest = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()[:12]
    return f"{prompt_version or 'none'}+schema.{name}@{digest}"


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
