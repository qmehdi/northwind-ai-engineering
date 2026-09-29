"""Claude through the Anthropic API directly, for the Judge on the Local track.

The Local track has no cloud account, and the Judge is the one role that calls a cloud
(ADR 0010, ADR 0011). `NW_ANTHROPIC_API_KEY` turns the role from the fake judge into
Claude; without it the harness runs judge-free.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from nw.llm.providers.anthropic_base import AnthropicMessagesProvider


class AnthropicApiProvider(AnthropicMessagesProvider):
    name = "anthropic-api"
    endpoint = "https://api.anthropic.com"

    def __init__(self, *, api_key: str, timeout_s: float = 60.0) -> None:
        super().__init__(AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0))
