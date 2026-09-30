"""Cost metering, a hard spend cap, and cost attributed to the run that spent it.

Every completion is priced as it returns. The cap is checked before a call is made, against
what has been spent plus a reservation for the call in flight, so a burst of concurrent calls
cannot all squeeze under the limit at once. A refusal that consumed tokens is priced too.

The meter keeps running totals (spend, usage, fallbacks, retries, breaker skips) and only the
most recent `max_records` records, so a long-lived service holds bounded memory and the cap
reads a number, not a sum over every call since startup.

The cap is per process: two instances of a service each allow `spend_cap_usd`. It is the
last line inside one process, not the budget. On a platform the per-tenant budget lives in
the model gateway (LiteLLM virtual keys, API Management quotas), which sees every instance.

Per-run cost: `LLMClient.cost_scope()` opens a `CostScope` that collects the records of every
call made inside it, in any task started from it (a context variable, like the correlation
ID). Concurrent runs on one client each see their own calls, never each other's.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
    refused: bool = False  # the provider refused; the tokens it consumed are still billed
    residency: str = "default"  # the residency zone the call was routed to


@dataclass
class CostScope:
    """The cost records of one run: every completion made inside `LLMClient.cost_scope()`."""

    records: list[CostRecord] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for r in self.records:
            total = total + r.usage
        return total

    @property
    def tokens(self) -> int:
        u = self.usage
        return u.input_tokens + u.output_tokens

    def __len__(self) -> int:
        return len(self.records)


_scopes: contextvars.ContextVar[tuple[CostScope, ...]] = contextvars.ContextVar(
    "nw_cost_scopes", default=()
)


@contextmanager
def cost_scope() -> Iterator[CostScope]:
    """Collect every cost record made in this block (nested scopes each get them)."""
    scope = CostScope()
    token = _scopes.set((*_scopes.get(), scope))
    try:
        yield scope
    finally:
        _scopes.reset(token)


def _attribute(record: CostRecord) -> None:
    for scope in _scopes.get():
        scope.records.append(record)


DEFAULT_MAX_RECORDS = 1000


@dataclass
class CostMeter:
    cap_usd: float | None = None
    records: list[CostRecord] = field(default_factory=list)
    max_records: int = DEFAULT_MAX_RECORDS  # the most recent records kept; totals cover all
    _reserved_usd: float = 0.0
    _total_usd: float = 0.0
    _total_usage: Usage = field(default_factory=Usage)
    _completions: int = 0
    _fallbacks: int = 0
    _retries: int = 0
    _refusals: int = 0
    _skipped: dict[str, int] = field(default_factory=dict)
    _by_model: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_usd(self) -> float:
        return self._total_usd

    @property
    def total_usage(self) -> Usage:
        return self._total_usage

    def reserve(self, estimate_usd: float) -> None:
        """Hold budget for a call about to be made. Raises if the cap would be exceeded."""
        with self._lock:
            if self.cap_usd is not None and self._total_usd + self._reserved_usd + estimate_usd > (
                self.cap_usd
            ):
                raise SpendCapExceeded(
                    f"spend cap {self.cap_usd:.4f} USD would be exceeded: "
                    f"spent {self._total_usd:.4f}, reserved {self._reserved_usd:.4f}, "
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
        refused: bool = False,
        residency: str = "default",
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
            refused=refused,
            residency=residency,
        )
        with self._lock:
            self._total_usd += rec.cost_usd
            self._total_usage = self._total_usage + usage
            self._completions += 1
            self._fallbacks += int(fallback)
            self._retries += attempts - 1
            self._refusals += int(refused)
            for m in skipped:
                self._skipped[m] = self._skipped.get(m, 0) + 1
            self._by_model[model] = self._by_model.get(model, 0.0) + rec.cost_usd
            self.records.append(rec)
            overflow = len(self.records) - self.max_records
            if overflow > 0:
                del self.records[:overflow]
        _attribute(rec)
        return rec

    @property
    def fallback_count(self) -> int:
        return self._fallbacks

    @property
    def retry_count(self) -> int:
        """Provider round trips beyond the first, over every completion."""
        return self._retries

    @property
    def refusal_count(self) -> int:
        return self._refusals

    def resilience(self) -> dict[str, Any]:
        """Fallbacks, retries and breaker skips as counts, for a log line or `/version`."""
        with self._lock:
            return {
                "completions": self._completions,
                "fallbacks": self._fallbacks,
                "retries": self._retries,
                "refusals": self._refusals,
                "breaker_skips": dict(self._skipped),
            }

    def by_model(self) -> dict[str, float]:
        with self._lock:
            return dict(self._by_model)
