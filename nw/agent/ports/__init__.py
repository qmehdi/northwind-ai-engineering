"""Framework ports: the same registry and the same adversarial set, through the
track's native agent framework. Strands on AWS, Agent Development Kit on GCP.

What a framework does not do for you, the bridge does: every tool result comes back
redacted (the agent path's redaction, account ids kept), screened when a screener is
configured, and wrapped in `<untrusted_data>` exactly as the hand-built loop wraps it, so
the system prompt's rule about untrusted data means the same thing in every runtime."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nw.agent.screen import Screener
from nw.agent.tools import Observation, Tool, ToolRegistry, untrusted


async def guarded(obs: Observation, screener: Screener | None = None) -> str:
    """A tool observation as a framework may hand it to a model: redacted, screened when a
    screener is configured, and wrapped as untrusted data; errors as a small JSON object."""
    from nw.agent.loop import _guard_observation

    if not obs.ok:
        return json.dumps({"error": obs.error, "detail": obs.content})
    content = await _guard_observation(obs.content, obs.ok, screener, run_id="port")
    return untrusted(content)


def function_for(
    tool: Tool, registry: ToolRegistry, *, sync: bool = False, screener: Screener | None = None
) -> Callable[..., Any]:
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
        return await guarded(obs, screener)

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
