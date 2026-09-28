"""Cost metering and a hard spend cap.

Every completion is priced as it returns. The cap is checked before a call is
made, against what has been spent plus a reservation for the call in flight, so
a burst of concurrent calls cannot all squeeze under the limit at once.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from nw.llm.errors import SpendCapExceeded
from nw.llm.prices import price_for
from nw.llm.types import Usage

_warned: set[str] = set()


def cost_usd(model: str, usage: Usage) -> float:
    price, fallback = price_for(model)
    if fallback and model not in _warned:
        _warned.add(model)
        from nw.logging import get_logger

        get_logger("nw.llm.cost").warning(
            "no price for model %s; using the fallback price, add it to nw/llm/prices.py", model
        )
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.output_tokens * price.output_per_mtok
        + usage.cache_read_tokens * price.cache_read_per_mtok
        + usage.cache_write_tokens * price.cache_write_per_mtok
    ) / 1_000_000


@dataclass
class CostRecord:
    request_id: str
    model: str
    usage: Usage
    cost_usd: float
    correlation_id: str | None = None
    role: str | None = None
    fallback: bool = False  # the fallback model answered, not the role's primary
    attempts: int = 1  # provider round trips this completion took, retries included
    skipped: tuple[str, ...] = ()  # models skipped because their circuit was open


@dataclass
class CostMeter:
    cap_usd: float | None = None
    records: list[CostRecord] = field(default_factory=list)
    _reserved_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for r in self.records:
            total = total + r.usage
        return total

    def reserve(self, estimate_usd: float) -> None:
        """Hold budget for a call about to be made. Raises if the cap would be exceeded."""
        with self._lock:
            if self.cap_usd is not None and self.total_usd + self._reserved_usd + estimate_usd > (
                self.cap_usd
            ):
                raise SpendCapExceeded(
                    f"spend cap {self.cap_usd:.4f} USD would be exceeded: "
                    f"spent {self.total_usd:.4f}, reserved {self._reserved_usd:.4f}, "
                    f"next call about {estimate_usd:.4f}"
                )
            self._reserved_usd += estimate_usd

    def release(self, estimate_usd: float) -> None:
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - estimate_usd)

    def record(
        self,
        request_id: str,
        model: str,
        usage: Usage,
        correlation_id: str | None = None,
        *,
        role: str | None = None,
        fallback: bool = False,
        attempts: int = 1,
        skipped: tuple[str, ...] = (),
    ) -> CostRecord:
        rec = CostRecord(
            request_id=request_id,
            model=model,
            usage=usage,
            cost_usd=cost_usd(model, usage),
            correlation_id=correlation_id,
            role=role,
            fallback=fallback,
            attempts=attempts,
            skipped=skipped,
        )
        with self._lock:
            self.records.append(rec)
        return rec

    @property
    def fallback_count(self) -> int:
        return sum(1 for r in self.records if r.fallback)

    @property
    def retry_count(self) -> int:
        """Provider round trips beyond the first, over every completion."""
        return sum(r.attempts - 1 for r in self.records)

    def resilience(self) -> dict[str, Any]:
        """Fallbacks, retries and breaker skips as counts, for a log line or `/version`."""
        skipped: dict[str, int] = {}
        for r in self.records:
            for m in r.skipped:
                skipped[m] = skipped.get(m, 0) + 1
        return {
            "completions": len(self.records),
            "fallbacks": self.fallback_count,
            "retries": self.retry_count,
            "breaker_skips": skipped,
        }

    def by_model(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records:
            out[r.model] = out.get(r.model, 0.0) + r.cost_usd
        return out
