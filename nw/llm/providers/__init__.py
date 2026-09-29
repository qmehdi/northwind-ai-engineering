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

SDKs are imported lazily inside each branch so the test suite, which only ever uses the
fake provider, never needs cloud packages to import cleanly. A `RoleRouter` keeps one
provider object in front of the LLMClient whatever the mix.
"""

from __future__ import annotations

from nw.config import ProviderMode, Settings, Track, is_claude
from nw.llm.provider import LLMProvider
from nw.llm.providers.router import RoleRouter, describe


def is_fake(model: str) -> bool:
    return model.startswith("fake-")


def make_provider(settings: Settings) -> LLMProvider:
    from nw.llm.providers.fake import FakeProvider

    if settings.provider is ProviderMode.FAKE:
        return FakeProvider()
    timeout = settings.request_timeout_s

    if settings.gateway_url:
        from nw.llm.providers.openai_compat import for_gateway

        return for_gateway(settings.gateway_url, settings.gateway_key, timeout_s=timeout)

    if settings.track == Track.AWS:
        from nw.llm.providers.bedrock import BedrockProvider
        from nw.llm.providers.bedrock_converse import BedrockConverseProvider

        claude = BedrockProvider(
            region=settings.aws_region, profile=settings.aws_profile, timeout_s=timeout
        )
        converse = BedrockConverseProvider(
            region=settings.aws_region, profile=settings.aws_profile, timeout_s=timeout
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


def describe_route(provider: LLMProvider, model: str) -> str:
    """Which provider and endpoint a model id resolves to, through a router or not."""
    if isinstance(provider, RoleRouter):
        return provider.describe(model)
    return describe(provider)


__all__ = ["RoleRouter", "describe_route", "is_fake", "make_provider"]
