"""Who this process serves: the tenant and the environment (ADR 0009).

`NW_TENANT` names the learner in cohort mode and is unset in solo mode; `NW_ENVIRONMENT` is the
platform's name (`northwind` by default, another word for a second deploy of the same stack).
Both travel on every log line once `bind_identity` has run, on every `drift_alert`, on every
`metrics_snapshot` line and on `/version`, so the platform's log-based metrics can group by
them and a cost line can be tied to a tenant.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from nw.logging import bind_static_fields

DEFAULT_TENANT = "solo"
DEFAULT_ENVIRONMENT = "northwind"


def identity(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{"tenant": ..., "environment": ...}` from the environment, with the solo defaults."""
    e = os.environ if env is None else env
    return {
        "tenant": (e.get("NW_TENANT") or "").strip() or DEFAULT_TENANT,
        "environment": (e.get("NW_ENVIRONMENT") or "").strip() or DEFAULT_ENVIRONMENT,
    }


def bind_identity(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read the identity once and put it on every log line of this process."""
    fields = identity(env)
    bind_static_fields(**fields)
    return fields
