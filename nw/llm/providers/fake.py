"""A scriptable provider for tests and the local track.

Script it with a list of outcomes, one per call, or a callable that decides
per call. An outcome is a string (returned as text), an exception (raised),
or a Completion. Every call yields to the event loop at least once, the way a
network call does. The provider records the concurrency high-water mark so
tests can prove the client's semaphore works.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from typing import Any

from nw.llm.types import Completion, Message, StopReason, ToolSpec, Usage

Outcome = str | Exception | Completion
Script = list[Outcome] | Callable[[list[Message], dict[str, Any]], Outcome]


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        script: Script | None = None,
        *,
        delay_s: float | Callable[[int], float] = 0.0,
        tokens_per_call: tuple[int, int] = (50, 20),
    ) -> None:
        self._script = script
        self._delay = delay_s
        self._tokens = tokens_per_call
        self._counter = itertools.count()
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.high_water = 0

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
        index = next(self._counter)
        kwargs = {
            "model": model,
            "system": system,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "index": index,
        }
        # Snapshot: callers may mutate their message list after the call (an agent loop does).
        self.calls.append({"messages": list(messages), **kwargs})
        self.in_flight += 1
        self.high_water = max(self.high_water, self.in_flight)
        started = time.perf_counter()
        try:
            delay = self._delay(index) if callable(self._delay) else self._delay
            # Always yield, even with no delay: a real provider awaits the network, so other
            # tasks run while a call is in flight. A fake that never yields hides every race
            # that depends on that (a budget checked before the call, a slot held too long).
            await asyncio.sleep(delay or 0)
            outcome = self._outcome(index, messages, kwargs)
        finally:
            self.in_flight -= 1
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Completion):
            return outcome
        return Completion(
            text=outcome,
            usage=Usage(
                input_tokens=self._tokens[0],
                output_tokens=self._tokens[1],
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
            request_id=f"fake-{index}",
            model=model,
            stop_reason=StopReason.END_TURN,
        )

    def _outcome(self, index: int, messages: list[Message], kwargs: dict[str, Any]) -> Outcome:
        if self._script is None:
            last = messages[-1].content or ""
            return f"echo:{last}"
        if callable(self._script):
            return self._script(messages, kwargs)
        if index < len(self._script):
            return self._script[index]
        return self._script[-1]
