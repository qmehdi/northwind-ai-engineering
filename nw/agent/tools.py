"""Tools: the registry, validation, execution, and the approval flag.

Tool design is API design. A tool has a narrow purpose, a schema the model can
satisfy, a descriptive error when it cannot, and a flag that says whether it
can be undone. Most agent failures are tool-design failures; the registry is
where they are caught, before the loop spins on bad arguments.

The registry also hardens every tool the same way, so no tool author has to:
a timeout per tool (`timeout_s`, default 30 seconds) reported as `tool_timeout`;
an optional retry (`attempts` with a `retry_if` predicate, jittered) for backends
that fail transiently; and a cap on the observation (`max_observation_chars`,
default 8000) so one giant result cannot fill the model's context.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from nw.llm.types import ToolSpec
from nw.telemetry import span

ToolFn = Callable[..., Any] | Callable[..., Awaitable[Any]]
RetryIf = Callable[[BaseException], bool]

DEFAULT_TIMEOUT_S = 30.0
MAX_OBSERVATION_CHARS = 8000
RETRY_JITTER_S = (0.05, 0.5)


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn
    requires_approval: bool = False
    idempotent: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    attempts: int = 1  # total tries; 2 means one retry
    retry_if: RetryIf | None = None  # which exceptions earn the retry; None means none do

    @property
    def spec(self) -> ToolSpec:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)


@dataclass
class Observation:
    tool: str
    ok: bool
    content: str
    latency_ms: float
    error: str | None = None
    pending_approval: bool = False
    truncated: bool = False
    attempts: int = 1


ObservationHook = Callable[[Observation], None]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    # Called with every Observation after a tool ran. The service registers its Prometheus
    # counters here; the registry itself knows nothing about metrics.
    hooks: list[ObservationHook] = field(default_factory=list)
    max_observation_chars: int = MAX_OBSERVATION_CHARS
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # tests swap it out

    def register(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self.tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        *,
        requires_approval: bool = False,
        idempotent: bool = True,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        attempts: int = 1,
        retry_if: RetryIf | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator. The function's single argument is the Pydantic args model."""

        def wrap(fn: ToolFn) -> ToolFn:
            params = inspect.signature(fn).parameters
            if len(params) != 1:
                raise TypeError(f"{name}: tool functions take exactly one argument, the args model")
            # Resolve string annotations (from __future__ import annotations) to the class.
            resolved = typing.get_type_hints(fn)
            args_model = resolved.get(next(iter(params)))
            if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
                raise TypeError(f"{name}: the argument must be annotated with a Pydantic model")
            self.register(
                Tool(
                    name,
                    description,
                    args_model,
                    fn,
                    requires_approval,
                    idempotent,
                    timeout_s=timeout_s,
                    attempts=attempts,
                    retry_if=retry_if,
                )
            )
            return fn

        return wrap

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def validate(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[Tool, BaseModel | None, str | None]:
        """Look the tool up and validate its arguments. Returns (tool, args, error)."""
        return self.tools[name], self.tools[name].args_model(**arguments), None  # registry

    async def execute(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> Observation:
        """Validate, gate, run. Errors become observations the model can read, never
        exceptions that kill the loop; a tool that raises is a tool with a bad contract."""
        started = time.perf_counter()
        with span(f"tool.{name}", **{"nw.tool": name}) as sp:
            obs = await self._execute(name, arguments, approved, started)
            sp.set_attribute("nw.ok", obs.ok)
            sp.set_attribute("nw.attempts", obs.attempts)
            if obs.error:
                sp.set_attribute("nw.error", obs.error)
            if obs.pending_approval:
                sp.set_attribute("nw.pending_approval", True)
            if obs.truncated:
                sp.set_attribute("nw.truncated", True)
        for hook in self.hooks:
            hook(obs)
        return obs

    async def _execute(
        self, name: str, arguments: dict[str, Any], approved: bool, started: float
    ) -> Observation:
        try:
            tool, args, error = self.validate(name, arguments)
        except KeyError as exc:
            return Observation(name, False, str(exc), _ms(started), error="unknown_tool")
        if error:
            return Observation(name, False, error, _ms(started), error="invalid_arguments")
        if tool.requires_approval and not approved:
            return Observation(
                name,
                True,
                f"{name} requires human approval and was recorded as a proposed action; "
                "it was not executed.",
                _ms(started),
                pending_approval=True,
            )
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await asyncio.wait_for(_call(tool, args), timeout=tool.timeout_s)
            except TimeoutError:
                return Observation(
                    name,
                    False,
                    f"{name} timed out after {tool.timeout_s:g}s; try a narrower request",
                    _ms(started),
                    error="tool_timeout",
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt < tool.attempts and tool.retry_if is not None and tool.retry_if(exc):
                    await self.sleep(random.uniform(*RETRY_JITTER_S))
                    continue
                return Observation(
                    name,
                    False,
                    f"{name} failed: {type(exc).__name__}: {exc}",
                    _ms(started),
                    error="tool_error",
                    attempts=attempt,
                )
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            content, truncated = clip(content, self.max_observation_chars)
            return Observation(
                name, True, content, _ms(started), truncated=truncated, attempts=attempt
            )


async def _call(tool: Tool, args: BaseModel | None) -> Any:
    """Run the tool function so `wait_for` can cut it off: a coroutine is awaited, a plain
    function runs in a worker thread, so a blocking backend cannot stall the loop past
    its timeout either."""
    if inspect.iscoroutinefunction(tool.fn):
        return await tool.fn(args)
    result = await asyncio.to_thread(tool.fn, args)
    if inspect.isawaitable(result):
        result = await result
    return result


TRUNCATED_MARKER = "\n[truncated: {total} chars, the first {kept} shown]"


def clip(content: str, limit: int) -> tuple[str, bool]:
    """Cap an observation, with a marker the model can read, so it knows the result is
    incomplete and can ask for less rather than reason over a cut."""
    if limit <= 0 or len(content) <= limit:
        return content, False
    return content[:limit] + TRUNCATED_MARKER.format(total=len(content), kept=limit), True


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


UNTRUSTED_OPEN = "<untrusted_data>"
UNTRUSTED_CLOSE = "</untrusted_data>"


def untrusted(text: str) -> str:
    """Wrap text that came from outside the system, so the prompt can say it is data.
    A closing tag inside the text is defused, so the data cannot end its own wrapper."""
    text = text.replace("</untrusted_data", "</untrusted_data_")
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"
