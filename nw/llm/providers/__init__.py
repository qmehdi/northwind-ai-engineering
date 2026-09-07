"""Provider factory. The only place a track name turns into a vendor client.

SDKs are imported lazily inside each branch so the test suite, which only ever
uses the fake provider, never needs cloud packages to import cleanly.
"""

from __future__ import annotations

from nw.config import Settings, Track
from nw.llm.provider import LLMProvider


def make_provider(settings: Settings) -> LLMProvider:
    if settings.track == Track.AWS:
        from nw.llm.providers.bedrock import BedrockProvider

        return BedrockProvider(
            region=settings.aws_region,
            profile=settings.aws_profile,
            timeout_s=settings.request_timeout_s,
        )
    if settings.track == Track.GCP:
        from nw.llm.providers.vertex import VertexProvider

        if not settings.gcp_project:
            raise ValueError("NW_GCP_PROJECT is required on the gcp track")
        return VertexProvider(
            project_id=settings.gcp_project,
            region=settings.gcp_region,
            timeout_s=settings.request_timeout_s,
        )
    from nw.llm.providers.fake import FakeProvider

    return FakeProvider()
