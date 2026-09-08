"""Framework ports: the same registry and the same adversarial set, through the
track's native agent framework. Strands on AWS, Agent Development Kit on GCP."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nw.agent.tools import Tool, ToolRegistry


def function_for(tool: Tool, registry: ToolRegistry, *, sync: bool = False) -> Callable[..., Any]:
    """A plain Python function with the tool's fields as typed keyword parameters.

    Frameworks build their tool schema from a function signature. The registry's
    schema comes from a Pydantic model. This bridges the two without duplicating
    a single field: the signature is generated from the model, and the body goes
    back through `registry.execute`, so validation and the approval gate still apply.
    """
    fields = tool.args_model.model_fields
    params = []
    annotations: dict[str, Any] = {}
    for name, f in fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.default
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=f.annotation
            )
        )
        annotations[name] = f.annotation

    async def run_async(**kwargs: Any) -> str:
        obs = await registry.execute(tool.name, kwargs, approved=False)
        return obs.content if obs.ok else json.dumps({"error": obs.error, "detail": obs.content})

    def run_sync(**kwargs: Any) -> str:
        import asyncio

        return asyncio.run(run_async(**kwargs))

    fn = run_sync if sync else run_async
    fn.__name__ = tool.name
    fn.__qualname__ = tool.name
    fn.__doc__ = (
        tool.description
        + "\n\nArgs:\n"
        + "\n".join(f"    {n}: {(f.description or n)}" for n, f in fields.items())
    )
    fn.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    fn.__annotations__ = {**annotations, "return": str}
    return fn


def is_irreversible(registry: ToolRegistry, name: str) -> bool:
    t = registry.tools.get(name)
    return bool(t and t.requires_approval)


def model_fields(model: type[BaseModel]) -> list[str]:
    return list(model.model_fields)
