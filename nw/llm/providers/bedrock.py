"""Claude on Amazon Bedrock, through the Anthropic SDK's Bedrock client.

Auth comes from the AWS credential chain (profile, environment, or the
execution role in the cloud). No key is ever read from a file here.
"""

from __future__ import annotations

from anthropic import AsyncAnthropicBedrockMantle

from nw.llm.providers.anthropic_base import AnthropicMessagesProvider


class BedrockProvider(AnthropicMessagesProvider):
    name = "bedrock"

    def __init__(self, *, region: str, profile: str | None = None, timeout_s: float = 60.0) -> None:
        # max_retries=0: the course's own retry loop is the one in charge, so its
        # behaviour is visible and testable. The SDK would otherwise retry silently.
        super().__init__(
            AsyncAnthropicBedrockMantle(
                aws_region=region, aws_profile=profile, timeout=timeout_s, max_retries=0
            )
        )
