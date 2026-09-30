"""Provider factory. The only place a track name turns into a vendor client.

`make_provider` chooses by provider mode, gateway and track (ADR 0010):

- `NW_PROVIDER=fake`: the fake provider for every role, on any track.
- `NW_GATEWAY_URL` set: every role goes through the model gateway, OpenAI-compatible,
  with the role's model id as the gateway model name and `NW_GATEWAY_KEY` as bearer.
- AWS: Claude ids on the Anthropic SDK's Bedrock client, everything else on Converse.
- GCP: Claude ids on the Anthropic SDK's Vertex client, everything else on the
  OpenAI-compatible managed API for open models.
- Azure: Claude ids on the Anthropic SDK's Foundry client, everything else on the Foundry
  resource's v1 OpenAI-compatible endpoint, both with Entra ID (or `NW_AZURE_FOUNDRY_KEY`).
  `NW_AZURE_APIM_GATEWAY_URL` puts API Management's AI gateway in front of both, with
  `NW_GATEWAY_KEY` as the tenant's subscription key; `NW_GATEWAY_URL` (LiteLLM) still wins.
- Local: Ollama, with `fake-*` ids on the fake provider (the Judge without a cloud key)
  and Claude ids on the Anthropic API when `NW_ANTHROPIC_API_KEY` is set.

EU residency (`nw.llm.residency`): on the three cloud tracks without a gateway the provider is
a `ResidencyRouter`, which sends a call made inside `bind_residency("eu")` to a second set of
providers built for the EU endpoints (Bedrock in `aws_eu_region`, Claude on Google's `eu`
multi-region, the EU Foundry resource), built on the first EU call. Through a gateway, on the
Local track and under the fake provider one provider serves both, and the EU model ids alone
make the difference (`Settings.eu_model_for`).

SDKs are imported lazily inside each branch so the test suite, which only ever uses the
fake provider, never needs cloud packages to import cleanly. A `RoleRouter` keeps one
provider object in front of the LLMClient whatever the mix.
"""

from __future__ import annotations

from collections.abc import Callable

from nw.config import ProviderMode, Residency, Settings, Track, is_claude
from nw.llm.errors import ResidencyError
from nw.llm.provider import LLMProvider
from nw.llm.providers.router import RoleRouter, close_all, describe
from nw.llm.types import Completion, Message, ToolSpec


def is_fake(model: str) -> bool:
    return model.startswith("fake-")


def make_provider(settings: Settings) -> LLMProvider:
    from nw.llm.providers.fake import FakeProvider

    if settings.provider is ProviderMode.FAKE:
        return FakeProvider()
    timeout = settings.request_timeout_s

    if settings.gateway_url:
        from nw.llm.providers.openai_compat import for_gateway

        gateway = for_gateway(settings.gateway_url, settings.gateway_key, timeout_s=timeout)
        # `fake-*` ids (the Local Judge without a Claude route, and its EU twin) are answered in
        # memory as without a gateway; the gateway has no route for them.
        return RoleRouter([(is_fake, FakeProvider())], default=gateway)

    default = _track_provider(settings)
    if settings.track is Track.LOCAL or not settings.residency_routing:
        return default
    return ResidencyRouter(default, lambda: make_eu_provider(settings))


def _track_provider(settings: Settings) -> LLMProvider:
    from nw.llm.providers.fake import FakeProvider

    timeout = settings.request_timeout_s
    if settings.track == Track.AWS:
        from nw.llm.providers.bedrock import BedrockProvider
        from nw.llm.providers.bedrock_converse import BedrockConverseProvider

        claude = BedrockProvider(
            region=settings.aws_region, profile=settings.aws_profile, timeout_s=timeout
        )
        converse = BedrockConverseProvider(
            region=settings.aws_region,
            profile=settings.aws_profile,
            timeout_s=timeout,
            max_concurrency=settings.max_concurrency,
        )
        return RoleRouter([(is_claude, claude)], default=converse)

    if settings.track == Track.GCP:
        from nw.llm.providers.openai_compat import for_google_maas
        from nw.llm.providers.vertex import VertexProvider

        if not settings.gcp_project:
            raise ValueError("NW_GCP_PROJECT is required on the gcp track")
        claude = VertexProvider(
            project_id=settings.gcp_project, region=settings.gcp_region, timeout_s=timeout
        )
        maas = for_google_maas(settings.gcp_project, settings.gcp_maas_region, timeout_s=timeout)
        return RoleRouter([(is_claude, claude)], default=maas)

    if settings.track == Track.AZURE:
        from nw.llm.providers.azure_foundry import for_apim, for_foundry

        if settings.azure_apim_gateway_url:
            maas, claude = for_apim(
                settings.azure_apim_gateway_url, settings.gateway_key, timeout_s=timeout
            )
        else:
            if not settings.azure_foundry_endpoint:
                raise ValueError(
                    "NW_AZURE_FOUNDRY_ENDPOINT (or NW_AZURE_APIM_GATEWAY_URL) is required on the "
                    "azure track"
                )
            maas, claude = for_foundry(
                settings.azure_foundry_endpoint,
                api_key=settings.azure_foundry_key,
                timeout_s=timeout,
            )
        return RoleRouter([(is_claude, claude)], default=maas)

    from nw.llm.providers.openai_compat import for_ollama

    rules: list = [(is_fake, FakeProvider())]
    if settings.anthropic_api_key:
        from nw.llm.providers.anthropic_api import AnthropicApiProvider

        rules.append(
            (is_claude, AnthropicApiProvider(api_key=settings.anthropic_api_key, timeout_s=timeout))
        )
    return RoleRouter(rules, default=for_ollama(settings.ollama_url, timeout_s=timeout))


def make_eu_provider(settings: Settings) -> LLMProvider:
    """The providers for EU-resident calls on a cloud track (ids in `nw.config.EU_MODELS`)."""
    timeout = settings.request_timeout_s
    if settings.track == Track.AWS:
        from nw.llm.providers.bedrock_converse import BedrockConverseProvider

        # Every EU role is on bedrock-runtime in one EU region: gpt-oss in-region, Claude and
        # Nova through their `eu.` geo profiles, all on Converse.
        return BedrockConverseProvider(
            region=settings.aws_eu_region,
            profile=settings.aws_profile,
            timeout_s=timeout,
            max_concurrency=settings.max_concurrency,
        )
    if settings.track == Track.GCP:
        from nw.llm.providers.vertex import VertexProvider

        if not settings.gcp_project:
            raise ValueError("NW_GCP_PROJECT is required on the gcp track")
        claude = VertexProvider(
            project_id=settings.gcp_project, region=settings.gcp_eu_region, timeout_s=timeout
        )
        return RoleRouter(
            [(is_claude, claude)],
            default=RefuseProvider(
                "no EU endpoint for open models on the gcp track; deploy one and put it "
                "behind the gateway (NW_GATEWAY_URL)"
            ),
        )
    if settings.track == Track.AZURE:
        if not settings.azure_foundry_eu_endpoint:
            return RefuseProvider(
                "NW_AZURE_FOUNDRY_EU_ENDPOINT is not set: no EU Foundry resource for EU calls"
            )
        from nw.llm.providers.azure_foundry import for_foundry

        maas, claude = for_foundry(settings.azure_foundry_eu_endpoint, timeout_s=timeout)
        return RoleRouter([(is_claude, claude)], default=maas)
    return _track_provider(settings)


class RefuseProvider:
    """A provider that refuses every call with `ResidencyError`: the EU side of a track with no
    EU endpoint for these models. Nothing is sent anywhere."""

    name = "refuse"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def complete(self, messages: list[Message], *, model: str, **_: object) -> Completion:
        from nw.logging import get_logger, log_fields

        get_logger("nw.llm.residency").warning(
            "residency refused", extra=log_fields(model=model, reason=self.reason)
        )
        raise ResidencyError(f"{model}: {self.reason}")


class ResidencyRouter:
    """The default providers, and the EU ones for calls made under `bind_residency("eu")`.
    The EU side is built on first use, so a track that never serves an EU account never
    constructs (or authenticates) its EU clients."""

    name = "residency-router"

    def __init__(self, default: LLMProvider, eu_factory: Callable[[], LLMProvider]) -> None:
        self.default = default
        self._eu_factory = eu_factory
        self._eu: LLMProvider | None = None

    @property
    def eu(self) -> LLMProvider:
        if self._eu is None:
            self._eu = self._eu_factory()
        return self._eu

    def for_residency(self, residency: Residency) -> LLMProvider:
        return self.eu if residency is Residency.EU else self.default

    def _current(self) -> LLMProvider:
        from nw.llm.residency import current_residency

        return self.for_residency(current_residency())

    def describe(self, model: str) -> str:
        return describe_route(self._current(), model)

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
        return await self._current().complete(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def aclose(self) -> None:
        await close_all([self.default, *([self._eu] if self._eu is not None else [])])


def describe_route(provider: LLMProvider, model: str) -> str:
    """Which provider and endpoint a model id resolves to, through a router or not."""
    if isinstance(provider, RoleRouter | ResidencyRouter):
        return provider.describe(model)
    return describe(provider)


__all__ = [
    "RefuseProvider",
    "ResidencyRouter",
    "RoleRouter",
    "describe_route",
    "is_fake",
    "make_eu_provider",
    "make_provider",
]
