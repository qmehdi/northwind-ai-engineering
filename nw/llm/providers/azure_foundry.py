"""Models on Microsoft Foundry (the Azure track, ADR 0013).

A Foundry resource answers two request shapes, both verified against Microsoft Learn on
2026-09-29:

- The v1 OpenAI-compatible API at `https://<resource>.services.ai.azure.com/openai/v1` (the
  `<resource>.openai.azure.com` host serves the same path), no `api-version`, for Azure OpenAI
  models and the other Foundry models sold by Azure, gpt-oss-120b included
  (learn.microsoft.com/azure/foundry/openai/api-version-lifecycle). The `model` field is the
  deployment name. Reasoning models reject `max_tokens` and sampling parameters, so this
  provider sends `max_completion_tokens` and drops `temperature` for them.
- The Anthropic Messages API at `https://<resource>.services.ai.azure.com/anthropic` for
  Claude, through the Anthropic SDK's `AsyncAnthropicFoundry` client
  (learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-claude).

Auth is Microsoft Entra ID by default: a token from `DefaultAzureCredential` for the scope
`https://ai.azure.com/.default`, the scope both pages use today (the older
`https://cognitiveservices.azure.com/.default` is still accepted by the OpenAI path). An API
key from Key Vault (`NW_AZURE_FOUNDRY_KEY`) is the alternative; Claude's Mythos models take
Entra ID only.

Behind API Management's AI gateway (`NW_AZURE_APIM_GATEWAY_URL`) the same two shapes are
published at `<gateway>/openai/v1` and `<gateway>/anthropic`, and the tenant's APIM
subscription key travels in the `api-key` header, the header APIM uses for imported Azure
OpenAI and Foundry APIs.

`azure-identity` is imported lazily, so the module imports on every track.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from nw.llm.providers.anthropic_base import AnthropicMessagesProvider
from nw.llm.providers.openai_compat import OpenAICompatProvider, build_request
from nw.llm.types import Message, ToolSpec

SCOPE = "https://ai.azure.com/.default"
# Model families on the v1 API that take `max_completion_tokens` only and reject sampling
# parameters (the reasoning models: the o-series and GPT-5 and later).
_REASONING = re.compile(r"^(o\d|gpt-5|gpt-6)", re.IGNORECASE)


def is_reasoning(model: str) -> bool:
    return bool(_REASONING.match(model.rsplit("/", 1)[-1]))


def resource_root(endpoint: str) -> str:
    """`https://<resource>.services.ai.azure.com` from any Foundry URL: the resource endpoint,
    a project endpoint (`.../api/projects/<name>`), or one with `/openai/v1` or `/anthropic`."""
    parts = urlsplit(endpoint.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"not a Foundry endpoint URL: {endpoint!r}")
    return f"{parts.scheme}://{parts.netloc}"


def openai_url(root: str) -> str:
    return f"{root.rstrip('/')}/openai/v1"


def anthropic_url(root: str) -> str:
    return f"{root.rstrip('/')}/anthropic"


class EntraTokenSource:
    """An Entra ID access token for `scope`, cached until five minutes before it expires.
    `credential` is any azure-identity credential; the default is `DefaultAzureCredential`
    (the developer's `az login` on a laptop, the managed identity in the cloud)."""

    def __init__(self, scope: str = SCOPE, credential: Any | None = None) -> None:
        self.scope = scope
        self._credential = credential
        self._token: Any | None = None

    def _get(self) -> str:
        import time

        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        if self._token is None or self._token.expires_on - 300 <= time.time():
            self._token = self._credential.get_token(self.scope)
        return self._token.token

    def sync(self) -> str:
        return self._get()

    async def __call__(self) -> str:
        return await asyncio.to_thread(self._get)


class AzureOpenAIProvider(OpenAICompatProvider):
    """The v1 chat completions endpoint of a Foundry resource (or the gateway in front of it).
    Differs from the generic endpoint in two places: a static key goes in `api-key`, and
    reasoning models get `max_completion_tokens` with no temperature."""

    async def _headers(self) -> dict[str, str]:
        if self._token_source is not None:
            token = await self._token_source()
            return {"authorization": f"Bearer {token}"} if token else {}
        return {"api-key": self._api_key} if self._api_key else {}

    def _build(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        return build_azure_request(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def _request_id(self, response: httpx.Response) -> str | None:
        return response.headers.get("apim-request-id") or response.headers.get("x-request-id")


def build_azure_request(
    messages: list[Message],
    *,
    model: str,
    system: str | None,
    tools: list[ToolSpec] | None,
    max_tokens: int,
    temperature: float | None,
) -> dict[str, Any]:
    """The chat completions body for the v1 API: `max_completion_tokens` for every model (the
    v1 API accepts it everywhere and reasoning models accept nothing else), and no
    temperature for reasoning models. Pure, so it is tested without a network."""
    body = build_request(
        messages,
        model=model,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
        temperature=None if is_reasoning(model) else temperature,
    )
    body["max_completion_tokens"] = body.pop("max_tokens")
    return body


class FoundryClaudeProvider(AnthropicMessagesProvider):
    """Claude on Foundry through `AsyncAnthropicFoundry`. With a key the SDK sends it as both
    `x-api-key` and `api-key`, so the same object works against the resource and against
    the APIM gateway."""

    name = "foundry-claude"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        token_source: EntraTokenSource | None = None,
        timeout_s: float = 60.0,
        client: Any | None = None,
    ) -> None:
        self.endpoint = base_url.rstrip("/")
        if client is None:
            from anthropic import AsyncAnthropicFoundry

            kwargs: dict[str, Any] = {"base_url": self.endpoint, "timeout": timeout_s}
            if api_key:
                kwargs["api_key"] = api_key
            else:
                kwargs["azure_ad_token_provider"] = token_source or EntraTokenSource()
            # max_retries=0: the course's own retry loop is the one in charge.
            client = AsyncAnthropicFoundry(max_retries=0, **kwargs)
        super().__init__(client)


# ----- the three ways in -------------------------------------------------------------------


def for_foundry(
    endpoint: str,
    *,
    api_key: str | None = None,
    timeout_s: float = 60.0,
    token_source: EntraTokenSource | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    claude_client: Any | None = None,
) -> tuple[AzureOpenAIProvider, FoundryClaudeProvider]:
    """The two providers of one Foundry resource: v1 chat completions and Claude. Entra ID
    unless `api_key` is given; one token source is shared by both."""
    root = resource_root(endpoint)
    tokens = None if api_key else (token_source or EntraTokenSource())
    openai = AzureOpenAIProvider(
        base_url=openai_url(root),
        api_key=api_key,
        token_source=tokens,
        timeout_s=timeout_s,
        name="foundry-openai",
        transport=transport,
    )
    claude = FoundryClaudeProvider(
        base_url=anthropic_url(root),
        api_key=api_key,
        token_source=tokens,
        timeout_s=timeout_s,
        client=claude_client,
    )
    return openai, claude


def for_apim(
    gateway_url: str,
    key: str | None,
    *,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    claude_client: Any | None = None,
) -> tuple[AzureOpenAIProvider, FoundryClaudeProvider]:
    """The two providers behind API Management: the same paths under the gateway, the
    tenant's subscription key in `api-key`."""
    root = gateway_url.rstrip("/")
    openai = AzureOpenAIProvider(
        base_url=openai_url(root),
        api_key=key,
        timeout_s=timeout_s,
        name="apim-openai",
        transport=transport,
    )
    claude = FoundryClaudeProvider(
        base_url=anthropic_url(root),
        api_key=key or "none",
        timeout_s=timeout_s,
        client=claude_client,
    )
    claude.name = "apim-claude"
    return openai, claude


__all__ = [
    "SCOPE",
    "AzureOpenAIProvider",
    "EntraTokenSource",
    "FoundryClaudeProvider",
    "anthropic_url",
    "build_azure_request",
    "for_apim",
    "for_foundry",
    "is_reasoning",
    "openai_url",
    "resource_root",
]
