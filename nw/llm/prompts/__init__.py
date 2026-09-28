"""The prompt registry: every system prompt and rubric the course uses, by name, with a hash.

A prompt is code that the model runs. Like code it needs a version, and unlike code its
version is not in git's hands once a service is deployed: two images built a week apart can
carry different rubrics and identical package versions. So every prompt is registered here
with the first twelve hex characters of its SHA-256, and the pair `name@hash` is the
`prompt_version` that travels with every Answer, every evaluation row, every eval.json and
every model-call span. When a number moves, the version says whether the prompt did.

    uv run python -m nw.llm.prompts          # the table: name, hash, length, first line

Register at module import, next to the text:

    ANSWER_PROMPT = register("policy.answer", SYSTEM)

and pass `ANSWER_PROMPT.text` to the client. `version_for(text)` looks a raw system string up
so a caller that only has the text (the agent loop, a notebook) still gets a version.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import sys
from dataclasses import dataclass

HASH_CHARS = 12

# Modules that register prompts at import. The CLI imports them so the table is complete;
# nothing else needs the list. `nw.agent.loop` is included best effort: its rules are
# registered from here by text, so the agent package does not depend on this module.
KNOWN_MODULES: tuple[str, ...] = ("nw.policy.answer", "nw.policy.evaluate")


def prompt_hash(text: str) -> str:
    """The first HASH_CHARS hex characters of the SHA-256 of the exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_CHARS]


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str

    @property
    def hash(self) -> str:
        return prompt_hash(self.text)

    @property
    def version(self) -> str:
        """`name@hash`: what travels in answers, reports and spans."""
        return f"{self.name}@{self.hash}"

    def __str__(self) -> str:  # so `system=str(PROMPT)` also works
        return self.text


_REGISTRY: dict[str, Prompt] = {}
_BY_HASH: dict[str, Prompt] = {}


def register(name: str, text: str) -> Prompt:
    """Register `text` under `name`. Re-registering identical text is a no-op (module reloads);
    a different text under an existing name is a bug and raises."""
    p = Prompt(name, text)
    existing = _REGISTRY.get(name)
    if existing is not None and existing.text != text:
        raise ValueError(
            f"prompt {name!r} is already registered with hash {existing.hash}; "
            f"new text hashes to {p.hash}. Give the new prompt its own name."
        )
    _REGISTRY[name] = p
    _BY_HASH[p.hash] = p
    return p


def get(name: str) -> Prompt:
    return _REGISTRY[name]


def registered() -> list[Prompt]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def versions() -> dict[str, str]:
    """`{name: hash}` for every registered prompt: what eval.json and the index manifest carry."""
    return {p.name: p.hash for p in registered()}


def version_for(text: str | None) -> str | None:
    """The version of a raw system string: `name@hash` when it is registered, otherwise
    `unregistered@hash`, so an unversioned prompt is visible as such in the trace. None for no
    system prompt at all."""
    if text is None:
        return None
    p = _BY_HASH.get(prompt_hash(text))
    return p.version if p else f"unregistered@{prompt_hash(text)}"


# ----- the service layer's own prompt --------------------------------------------------

STRUCTURED_INSTRUCTIONS = register(
    "llm.structured",
    "Reply with a single JSON object and nothing else: no prose, no code fence. "
    "It must validate against this JSON Schema:",
)


# ----- CLI ----------------------------------------------------------------------------


def load_known() -> list[str]:
    """Import every module that registers prompts; return the names that could not be imported."""
    missing = []
    for mod in KNOWN_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{mod}: {exc}")
    try:  # the agent's rules, by text, without making nw.agent depend on this module
        loop = importlib.import_module("nw.agent.loop")
        register("agent.rules", loop.SYSTEM_RULES)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"nw.agent.loop: {exc}")
    return missing


def format_table(prompts: list[Prompt]) -> str:
    rows = ["| Prompt | Hash | Chars | First line |", "| --- | --- | ---: | --- |"]
    for p in prompts:
        first = p.text.strip().splitlines()[0] if p.text.strip() else ""
        rows.append(f"| {p.name} | {p.hash} | {len(p.text)} | {first[:60]} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    """`python -m nw.llm.prompts` (see `__main__.py`): the table, or `--json` for {name: hash}."""
    ap = argparse.ArgumentParser(description="List the registered prompts and their hashes.")
    ap.add_argument("--json", action="store_true", help="print {name: hash} as JSON")
    args = ap.parse_args(argv)
    missing = load_known()
    if args.json:
        import json

        print(json.dumps(versions(), indent=1))
    else:
        print(format_table(registered()))
    for m in missing:
        print(f"not loaded: {m}", file=sys.stderr)
    return 0
