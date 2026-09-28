"""The agent version: one hash over everything that changes how the agent behaves.

A model artifact has a version because its weights are a file. An agent has no such
file: its behaviour is the system prompt, the tools it can see (names, descriptions,
schemas) and the model ids it calls. Change any of them and the adversarial pass rate,
the cost per resolution and the escalation decisions can move. So the version is a
hash over exactly those three things, recorded on every trajectory, in the evaluation
report, on the `agent.run` span and on the service's `/version`, and a trace from last
week can be tied to the configuration that produced it.

`prompt_version` gives the prompt a `name@hash` the way `nw.llm.prompts` does; it uses
that module's hash when it is importable so the two never disagree on a prompt's version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from nw.llm.types import ToolSpec


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


try:  # the service layer's prompt registry, when present
    from nw.llm.prompts import prompt_hash
except ImportError:  # pragma: no cover
    prompt_hash = _sha


def prompt_version(name: str, text: str) -> str:
    """`name@hash` for a prompt text, so a log line can say which prompt ran."""
    return f"{name}@{prompt_hash(text)}"


def tool_fingerprint(specs: Iterable[ToolSpec]) -> str:
    """Names, descriptions and schemas, in name order so registration order does not matter."""
    canon = sorted(
        ({"name": s.name, "description": s.description, "schema": s.input_schema} for s in specs),
        key=lambda d: d["name"],
    )
    return _sha(json.dumps(canon, sort_keys=True, default=str))


def agent_version(system: str, specs: Iterable[ToolSpec], models: dict[str, str]) -> str:
    """Twelve hex characters over the prompt, the tool specs and the model ids in use."""
    return describe(system, specs, models)["agent_version"]


def describe(system: str, specs: Iterable[ToolSpec], models: dict[str, str]) -> dict[str, Any]:
    """The version and its parts, for `/version` and for a person reading a report."""
    specs = list(specs)
    prompt = prompt_version("system", system)
    tools = tool_fingerprint(specs)
    payload = json.dumps(
        {"prompt": prompt, "tools": tools, "models": dict(sorted(models.items()))}, sort_keys=True
    )
    return {
        "agent_version": _sha(payload),
        "prompt": prompt,
        "tools_version": tools,
        "tools": sorted(s.name for s in specs),
        "models": dict(sorted(models.items())),
    }
