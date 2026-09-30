"""One real model call on the configured track (the first part, step 8).

Spends a fraction of a cent."""

import asyncio

from nw.config import settings
from nw.llm import LLMClient
from nw.llm.providers import make_provider
from nw.logging import bind_correlation_id, configure_logging

configure_logging("json")
s = settings()
client = LLMClient(make_provider(s), settings=s)


async def main() -> None:
    with bind_correlation_id("live-check"):
        c = await client.complete(
            "Name one thing a support ticket should always include.", max_tokens=60
        )
    print(c.text)
    print(f"spend so far: {client.spend_usd:.6f} USD")


asyncio.run(main())
