"""Tools: the registry, validation, execution, and the approval flag.

Tool design is API design. A tool has a narrow purpose, a schema the model can
satisfy, a descriptive error when it cannot, and a flag that says whether it
can be undone. Most agent failures are tool-design failures; the registry is
where they are caught, before the loop spins on bad arguments.
"""

from __future__ import annotations

import inspect
import json
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from nw.llm.types import ToolSpec
from nw.telemetry import span

ToolFn = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn
    requires_approval: bool = False
    idempotent: bool = True

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


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

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
            self.register(Tool(name, description, args_model, fn, requires_approval, idempotent))
            return fn

        return wrap

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def validate(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[Tool, BaseModel | None, str | None]:
        """Look the tool up and validate its arguments. Returns (tool, args, error)."""
        return self.tools[name], self.tools[name].args_model(**arguments), None  # Step 2

    async def execute(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> Observation:
        """Validate, gate, run. Errors become observations the model can read, never
        exceptions that kill the loop; a tool that raises is a tool with a bad contract."""
        started = time.perf_counter()
        with span(f"tool.{name}", **{"nw.tool": name}) as sp:
            obs = await self._execute(name, arguments, approved, started)
            sp.set_attribute("nw.ok", obs.ok)
            if obs.error:
                sp.set_attribute("nw.error", obs.error)
            if obs.pending_approval:
                sp.set_attribute("nw.pending_approval", True)
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
        try:
            result = tool.fn(args)
            if inspect.isawaitable(result):
                result = await result
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            return Observation(name, True, content, _ms(started))
        except Exception as exc:  # noqa: BLE001
            return Observation(
                name,
                False,
                f"{name} failed: {type(exc).__name__}: {exc}",
                _ms(started),
                error="tool_error",
            )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


UNTRUSTED_OPEN = "<untrusted_data>"
UNTRUSTED_CLOSE = "</untrusted_data>"


def untrusted(text: str) -> str:
    """Wrap text that came from outside the system, so the prompt can say it is data.
    A closing tag inside the text is defused, so the data cannot end its own wrapper."""
    text = text.replace("</untrusted_data", "</untrusted_data_")
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"
