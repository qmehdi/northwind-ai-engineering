"""Claude on Google Cloud Vertex AI, through the Anthropic SDK's Vertex client.

Auth is Application Default Credentials: `gcloud auth application-default login`
on a laptop, the service account in the cloud.
"""

from __future__ import annotations

from anthropic import AsyncAnthropicVertex

from nw.llm.providers.anthropic_base import AnthropicMessagesProvider


class VertexProvider(AnthropicMessagesProvider):
    name = "vertex"

    def __init__(self, *, project_id: str, region: str = "global", timeout_s: float = 60.0) -> None:
        super().__init__(
            AsyncAnthropicVertex(
                project_id=project_id, region=region, timeout=timeout_s, max_retries=0
            )
        )
