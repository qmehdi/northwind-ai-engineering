"""Residency routing: an EU account's data goes only to EU-resident models (06 H5).

Northwind's own policy (`data/policies/gdpr-data-residency.md`) promises EU customers that
their content is processed only in the EU, and 67 of the 240 accounts in `data/accounts.json`
are `region: eu`. The model call is where that promise is most easily broken: the track's
default ids run in the US (or globally), so the client chooses the model id and the endpoint by
the residency of the data it is about to send.

Residency travels like the correlation ID, as a context variable: whoever knows the account
(the agent run, the policy request) binds it once, and every model call made inside the block,
in any task started from it, uses the EU ids and the EU endpoint:

    with bind_residency(residency_for_account("NW-10042")):
        await run_agent(ticket, registry, client)

`nw.config.EU_MODELS` holds the ids per track and says where each was verified. A role with no
EU model on the track raises `ResidencyError` before any network; nothing falls back to a model
outside the zone.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from nw.config import Residency

ACCOUNTS = Path(__file__).resolve().parents[2] / "data" / "accounts.json"

_residency: contextvars.ContextVar[Residency] = contextvars.ContextVar(
    "nw_residency", default=Residency.DEFAULT
)


def current_residency() -> Residency:
    return _residency.get()


@contextmanager
def bind_residency(value: Residency | str | None) -> Iterator[Residency]:
    """Set the residency for the duration of a block. None or an unknown value is the default."""
    residency = residency_of(value)
    token = _residency.set(residency)
    try:
        yield residency
    finally:
        _residency.reset(token)


def residency_of(value: Residency | str | None) -> Residency:
    """`eu` (any case) is EU; everything else (`us`, `apac`, None) is the default."""
    if isinstance(value, Residency):
        return value
    is_eu = str(value or "").strip().lower() == Residency.EU.value
    return Residency.EU if is_eu else Residency.DEFAULT


@lru_cache(maxsize=4)
def _regions(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    rows = json.loads(p.read_text(encoding="utf-8"))
    return {str(r["account_id"]): str(r.get("region") or "") for r in rows if "account_id" in r}


def residency_for_account(account_id: str | None, *, accounts: Path = ACCOUNTS) -> Residency:
    """The residency of an account from its `region` in `data/accounts.json`. An unknown
    account is the default: the caller that has no account has nothing to bind."""
    if not account_id:
        return Residency.DEFAULT
    return residency_of(_regions(str(accounts)).get(account_id.strip().upper()))


__all__ = [
    "ACCOUNTS",
    "Residency",
    "bind_residency",
    "current_residency",
    "residency_for_account",
    "residency_of",
]
