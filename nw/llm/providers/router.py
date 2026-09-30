"""One provider object that dispatches per model id.

The LLMClient holds one provider. On a track without a gateway the roles live on
different vendors' endpoints (Claude on the Anthropic SDK, gpt-oss and Nova on Converse
or the OpenAI-compatible endpoint, the fake models in memory), so the router picks the
underlying provider from the model id and forwards the call unchanged. Errors come back
already classified by the provider that raised them.
"""

from __future__ import annotations

from collections.abc import Callable

from nw.llm.provider import LLMProvider
from nw.llm.types import Completion, Message, ToolSpec

Rule = tuple[Callable[[str], bool], LLMProvider]


class RoleRouter:
    name = "router"

    def __init__(self, rules: list[Rule], *, default: LLMProvider) -> None:
        self._rules = rules
        self._default = default

    def route(self, model: str) -> LLMProvider:
        for matches, provider in self._rules:
            if matches(model):
                return provider
        return self._default

    def describe(self, model: str) -> str:
        """`<provider name> at <endpoint>` for a model id, for preflight and `/version`."""
        return describe(self.route(model))

    @property
    def providers(self) -> list[LLMProvider]:
        seen: list[LLMProvider] = []
        for _, p in [*self._rules, (None, self._default)]:
            if p not in seen:
                seen.append(p)
        return seen

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
        return await self.route(model).complete(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def aclose(self) -> None:
        await close_all(self.providers)


async def close_all(providers: list[LLMProvider]) -> None:
    """`aclose()` on every provider that has one, each once."""
    seen: list[int] = []
    for p in providers:
        if id(p) in seen:
            continue
        seen.append(id(p))
        close = getattr(p, "aclose", None)
        if close is not None:
            await close()


def describe(provider: LLMProvider) -> str:
    endpoint = getattr(provider, "endpoint", None)
    return f"{provider.name} at {endpoint}" if endpoint else provider.name
