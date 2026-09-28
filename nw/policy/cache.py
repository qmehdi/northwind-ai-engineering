"""An exact-match response cache for the policy service.

Off by default (`NW_POLICY_CACHE_TTL_S=0`). When on, an answer is served again for the same
question, audience and k, from the same index, prompt and model, until the TTL runs out or
the entry is evicted. The key includes the index manifest hash and the prompt version on
purpose: a rebuild or a prompt change empties the cache without anyone remembering to.

Exact match only. A semantic cache (serve the nearest cached question) would hand a
paraphrase the answer to a different policy question, and a policy answer that is nearly
right is wrong. The cost lever for near-duplicates is the provider's prompt cache, which is
already on for the system prompt; this cache is for the same question asked twice.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable

_WS = re.compile(r"\s+")
_TRAIL = re.compile(r"[\s?.!]+$")


def normalise(question: str) -> str:
    """Case, surrounding whitespace, runs of whitespace and trailing punctuation do not make a
    different question. Anything else does."""
    return _TRAIL.sub("", _WS.sub(" ", question.strip().casefold()))


class ResponseCache[T]:
    def __init__(
        self, ttl_s: float = 0.0, size: int = 1000, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = ttl_s
        self.size = size
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_env(cls) -> ResponseCache[T]:
        return cls(
            ttl_s=float(os.environ.get("NW_POLICY_CACHE_TTL_S", "0")),
            size=int(os.environ.get("NW_POLICY_CACHE_SIZE", "1000")),
        )

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0 and self.size > 0

    @staticmethod
    def key(
        question: str, audience: str, k: int, index_hash: str, prompt_version: str, model_id: str
    ) -> str:
        raw = "\x1f".join(
            [normalise(question), audience, str(k), index_hash, prompt_version, model_id]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def get(self, key: str) -> T | None:
        if not self.enabled:
            return None
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires, value = entry
        if self._clock() >= expires:
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: T) -> None:
        if not self.enabled:
            return
        self._entries[key] = (self._clock() + self.ttl_s, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.size:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)

    def stats(self) -> dict[str, float | int | bool]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "ttl_s": self.ttl_s,
            "size": self.size,
            "entries": len(self),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
        }
