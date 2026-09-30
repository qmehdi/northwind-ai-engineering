"""Retry policy: exponential backoff with full jitter, honouring Retry-After."""

from __future__ import annotations

import random

from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=4, ge=1)
    base_delay_s: float = Field(default=0.5, gt=0)
    max_delay_s: float = Field(default=8.0, gt=0)
    retry_after_cap_s: float = Field(default=30.0, gt=0)

    def delay_for(self, attempt: int, retry_after_s: float | None = None) -> float:
        """Seconds to wait before `attempt` (1-based: the delay before the second try
        is `delay_for(1)`).

        Rules:
        - If the provider sent Retry-After, wait that long, capped at `retry_after_cap_s`.
          Ignoring it is how you get banned from a provider.
        - Otherwise exponential backoff with full jitter: uniform between 0 and
          min(max_delay, base * 2**(attempt-1)). Full jitter spreads a thundering herd
          better than equal jitter; see the AWS Architecture Blog post
          "Exponential Backoff And Jitter".
        """
        # SOLUTION BEGIN
        if retry_after_s is not None:
            return min(max(retry_after_s, 0.0), self.retry_after_cap_s)
        ceiling = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return random.uniform(0.0, ceiling)
        # STUB: raise NotImplementedError("Service layer, backoff: Retry-After, then full jitter")
        # SOLUTION END
