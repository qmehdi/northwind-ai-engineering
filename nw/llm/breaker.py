"""A circuit breaker per model id, so a dead model is skipped instead of paid for.

Closed: calls go through. After `threshold` consecutive failures the circuit opens and
every call to that model is refused at once, for `open_s` seconds, without a network
round trip and without the retry loop's back-off. When the window has passed the circuit
is half open: one probe call is let through; success closes it, failure opens it again
for another window. The client consults it before every attempt and asks for the
fallback model when the primary is open.

State lives in the process. Two instances of a service open and close their own circuits,
which is what you want: the failure they see may be their own region's.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    probing: bool = False


@dataclass
class CircuitBreaker:
    threshold: int = 3
    open_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _circuits: dict[str, _Circuit] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def state(self, model: str) -> str:
        with self._lock:
            return self._state(self._circuits.get(model, _Circuit()))

    def _state(self, c: _Circuit) -> str:
        if c.opened_at is None:
            return CLOSED
        if self.clock() - c.opened_at >= self.open_s:
            return HALF_OPEN
        return OPEN

    def allow(self, model: str) -> bool:
        """True when a call to `model` may go on the wire now. A half-open circuit lets
        exactly one probe through until it reports back."""
        with self._lock:
            c = self._circuits.setdefault(model, _Circuit())
            s = self._state(c)
            if s == CLOSED:
                return True
            if s == HALF_OPEN and not c.probing:
                c.probing = True
                return True
            return False

    def success(self, model: str) -> None:
        with self._lock:
            self._circuits[model] = _Circuit()

    def failure(self, model: str) -> None:
        with self._lock:
            c = self._circuits.setdefault(model, _Circuit())
            c.failures += 1
            c.probing = False
            if c.opened_at is not None or c.failures >= self.threshold:
                c.opened_at = self.clock()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """For `/version` and a log line: every model the breaker has seen."""
        with self._lock:
            return {
                m: {"state": self._state(c), "consecutive_failures": c.failures}
                for m, c in self._circuits.items()
            }
