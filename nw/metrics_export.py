"""Metrics that reach the cloud without a scraper.

Every service keeps Prometheus counters in process and serves them on `/metrics`.
Nothing scrapes a Lambda sandbox or a Cloud Run instance, so those counters would die
with the instance. This module turns a curated slice of the registry into one structured
log line every `NW_METRICS_EXPORT_S` seconds (default 60, 0 disables) and lets the
platform's log pipeline turn the line into metrics:

- `NW_METRICS_FORMAT=emf` (set by the CDK stack): CloudWatch Embedded Metric Format. The
  `_aws` block names the keys that are metrics; CloudWatch Logs publishes them into the
  `Northwind` namespace with the dimensions `Service` and `Stage`. No agent, no
  `PutMetricData` call, no extra IAM.
- `NW_METRICS_FORMAT=json` (set by Terraform, and the default): a plain JSON line with
  `msg` `metrics_snapshot` and numeric fields. Log-based metrics in
  `deploy/gcp/session/main.tf` extract them with `EXTRACT(jsonPayload.<field>)`.

Counters are exported as the change since the previous line, so a Sum over a minute is
that minute's count. The p95 is computed from the histogram buckets of the same interval.
Gauges (drift level) are the value at the time of the line. The allowlist in `SERIES` is
the whole set: a service exports at most `MAX_FIELDS` numbers whatever its registry
holds, so the cardinality in the cloud is bounded by construction, not by discipline.

On Lambda the sandbox is frozen between invocations, so the timer only advances while a
request is in flight: the line for an interval is written during the first request after
the interval has passed, and a function nobody calls exports nothing, which is also what
it should cost.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO

from prometheus_client import REGISTRY, CollectorRegistry

from nw.logging import get_logger

log = get_logger("nw.metrics_export")

NAMESPACE = "Northwind"
MESSAGE = "metrics_snapshot"
MAX_FIELDS = 12
TERMINATIONS = ("answer", "max_steps", "budget", "error")


@dataclass(frozen=True)
class Series:
    """The Prometheus families one service exports and how each is read."""

    requests: str  # a counter family; every label set counts as a request
    errors: Mapping[str, tuple[str, ...]]  # label name to the values that count as an error
    latency: str  # a histogram family, seconds
    drift: str  # a gauge family, the current value
    cost: str | None = None  # a cumulative family summed over its label sets
    terminations: str | None = None  # a counter with a `terminated` label
    tool_calls: str | None = None  # a counter with an `outcome` label


SERIES: dict[str, Series] = {
    "triage": Series(
        "nw_triage_requests_total",
        {"outcome": ("not_ready",)},
        "nw_triage_latency_seconds",
        "nw_triage_drift_level",
    ),
    "semantic": Series(
        "nw_semantic_requests_total",
        {"outcome": ("not_ready",)},
        "nw_semantic_latency_seconds",
        "nw_semantic_drift_level",
    ),
    "policy": Series(
        "nw_policy_requests_total",
        {"outcome": ("not_ready",)},
        "nw_policy_latency_seconds",
        "nw_policy_drift_level",
        cost="nw_policy_spend_usd_total",
    ),
    "agent": Series(
        "nw_agent_runs_total",
        {"terminated": ("error",)},
        "nw_agent_latency_seconds",
        "nw_agent_drift_level",
        cost="nw_agent_cost_usd_total",
        terminations="nw_agent_terminations_total",
        tool_calls="nw_agent_tool_calls_total",
    ),
}

# Every field a line may carry, with its CloudWatch unit. Nothing outside this table is
# ever exported.
FIELDS: dict[str, str] = {
    "requests": "Count",
    "errors": "Count",
    "latency_count": "Count",
    "latency_sum_s": "Seconds",
    "latency_p95_ms": "Milliseconds",
    "cost_usd": "None",
    "drift_level": "None",
    **{f"runs_{t}": "Count" for t in TERMINATIONS},
    "tool_errors": "Count",
}
assert len(FIELDS) == MAX_FIELDS


def emf_name(field: str) -> str:
    """`latency_p95_ms` becomes `LatencyP95Ms`: the metric name CloudWatch shows."""
    return "".join(part.capitalize() for part in field.split("_"))


def _samples(registry: CollectorRegistry) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for metric in registry.collect():
        for s in metric.samples:
            out.setdefault(s.name, []).append((dict(s.labels), float(s.value)))
    return out


def p95_from_buckets(buckets: Mapping[float, float], count: float) -> float | None:
    """Linear interpolation inside the first cumulative bucket that holds the 95th
    observation; the last finite bound when it falls in the +Inf bucket. Seconds in,
    milliseconds out. None when the interval had no observation."""
    if count <= 0:
        return None
    target = 0.95 * count
    lower, prev = 0.0, 0.0
    last_finite = 0.0
    for bound in sorted(buckets):
        cum = buckets[bound]
        if math.isinf(bound):
            return round(last_finite * 1000, 3)
        if cum >= target:
            width = cum - prev
            frac = (target - prev) / width if width > 0 else 1.0
            return round((lower + (bound - lower) * frac) * 1000, 3)
        lower, prev, last_finite = bound, cum, bound
    return round(last_finite * 1000, 3)


class Exporter:
    """Reads one service's allowlisted series and builds the line for the track."""

    def __init__(
        self,
        service: str,
        *,
        stage: str = "",
        fmt: str = "json",
        registry: CollectorRegistry = REGISTRY,
        stream: TextIO | None = None,
        tenant: str = "",
        environment: str = "",
    ) -> None:
        self.service = service
        self.series = SERIES[service]
        self.stage = stage
        # The tenant and environment of a cohort platform (ADR 0009): plain fields on the
        # line, never dimensions, so the metric cardinality stays what the allowlist says
        # and the log-based metrics can still filter or group by them.
        self.tenant = tenant
        self.environment = environment
        self.fmt = fmt
        self.registry = registry
        self.stream = stream
        self.previous = self._raw()

    # ----- reading the registry ---------------------------------------------------

    def _raw(self) -> dict[str, Any]:
        s = self.series
        samples = _samples(self.registry)

        def total(name: str) -> float:
            return sum(v for _, v in samples.get(name, ()))

        def matching(name: str, label: str, values: tuple[str, ...]) -> float:
            return sum(v for lbl, v in samples.get(name, ()) if lbl.get(label) in values)

        buckets: dict[float, float] = {}
        for lbl, v in samples.get(f"{s.latency}_bucket", ()):
            bound = float(lbl.get("le", "inf"))
            buckets[bound] = buckets.get(bound, 0.0) + v
        drift = samples.get(s.drift)
        raw: dict[str, Any] = {
            "requests": total(s.requests),
            "errors": sum(matching(s.requests, k, v) for k, v in s.errors.items()),
            "latency_count": total(f"{s.latency}_count"),
            "latency_sum_s": total(f"{s.latency}_sum"),
            "buckets": buckets,
            "drift_level": drift[0][1] if drift else None,
        }
        if s.cost:
            raw["cost_usd"] = total(s.cost)
        if s.terminations:
            for t in TERMINATIONS:
                raw[f"runs_{t}"] = matching(s.terminations, "terminated", (t,))
        if s.tool_calls:
            raw["tool_errors"] = matching(s.tool_calls, "outcome", ("error",))
        return raw

    def snapshot(self) -> dict[str, float]:
        """The interval's numbers, allowlisted, counters as deltas."""
        cur = self._raw()
        prev = self.previous
        self.previous = cur
        out: dict[str, float] = {}
        for key in FIELDS:
            if key in ("latency_p95_ms", "drift_level") or key not in cur:
                continue
            out[key] = round(max(cur[key] - prev.get(key, 0.0), 0.0), 6)
        delta_buckets = {
            b: max(v - prev["buckets"].get(b, 0.0), 0.0) for b, v in cur["buckets"].items()
        }
        p95 = p95_from_buckets(delta_buckets, out.get("latency_count", 0.0))
        if p95 is not None:
            out["latency_p95_ms"] = p95
        if cur["drift_level"] is not None:
            out["drift_level"] = cur["drift_level"]
        assert set(out) <= set(FIELDS) and len(out) <= MAX_FIELDS
        return out

    # ----- the line ---------------------------------------------------------------

    def line(self, fields: Mapping[str, float] | None = None) -> dict[str, Any]:
        fields = self.snapshot() if fields is None else dict(fields)
        now_ms = int(time.time() * 1000)
        if self.fmt == "emf":
            line: dict[str, Any] = {
                "_aws": {
                    "Timestamp": now_ms,
                    "CloudWatchMetrics": [
                        {
                            "Namespace": NAMESPACE,
                            "Dimensions": [["Service", "Stage"]],
                            "Metrics": [{"Name": emf_name(k), "Unit": FIELDS[k]} for k in fields],
                        }
                    ],
                },
                "Service": self.service,
                # A dimension value cannot be empty; the CDK dashboard uses the same word.
                "Stage": self.stage or "default",
                "msg": MESSAGE,
                "tenant": self.tenant,
                "environment": self.environment,
            }
            line.update({emf_name(k): v for k, v in fields.items()})
            return line
        return {
            "msg": MESSAGE,
            "severity": "INFO",
            "service": self.service,
            "stage": self.stage,
            "tenant": self.tenant,
            "environment": self.environment,
            "ts": now_ms,
            **fields,
        }

    def emit(self) -> None:
        try:
            stream = self.stream or sys.stderr
            stream.write(json.dumps(self.line(), default=float) + "\n")
            stream.flush()
        except Exception as exc:  # noqa: BLE001
            log.warning("metrics export failed: %s", exc)


# ----- the background task -----------------------------------------------------------


async def _run(exporter: Exporter, interval: float) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            exporter.emit()
    except asyncio.CancelledError:
        exporter.emit()  # the last interval, on shutdown
        raise


def start_metrics_export(
    service: str,
    *,
    env: Mapping[str, str] | None = None,
    registry: CollectorRegistry = REGISTRY,
    stream: TextIO | None = None,
) -> asyncio.Task[None] | None:
    """One call from a service's lifespan. Returns the task, or None when disabled
    (`NW_METRICS_EXPORT_S=0`) or the service has no allowlist. The task ends with the
    event loop: uvicorn and the test client cancel it on shutdown, and the cancellation
    writes the final line."""
    env = os.environ if env is None else env
    interval = float(env.get("NW_METRICS_EXPORT_S", "60") or 0)
    name = service.removeprefix("northwind-")
    if interval <= 0:
        log.info("metrics export off")
        return None
    if name not in SERIES:
        log.warning("metrics export: no allowlist for %s, nothing exported", service)
        return None
    exporter = Exporter(
        name,
        stage=env.get("NW_STAGE", ""),
        fmt=env.get("NW_METRICS_FORMAT", "json"),
        registry=registry,
        stream=stream,
        tenant=env.get("NW_TENANT", "") or "solo",
        environment=env.get("NW_ENVIRONMENT", "") or "northwind",
    )
    task = asyncio.get_running_loop().create_task(
        _run(exporter, interval), name=f"metrics-export-{name}"
    )
    log.info("metrics export every %ss as %s", int(interval), exporter.fmt)
    return task
