"""The metrics exporter: a bounded, allowlisted line per interval, EMF on AWS, JSON on GCP."""

import asyncio
import io
import json

import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from nw import metrics_export as mx

pytestmark = pytest.mark.session01


def agent_registry() -> tuple[CollectorRegistry, dict]:
    """The agent service's families, on a private registry, with some traffic recorded."""
    reg = CollectorRegistry()
    runs = Counter("nw_agent_runs_total", "", ["role", "terminated"], registry=reg)
    latency = Histogram(
        "nw_agent_latency_seconds", "", ["role"], buckets=(1, 2, 5, 10, 20, 40, 80), registry=reg
    )
    cost = Counter("nw_agent_cost_usd_total", "", ["role"], registry=reg)
    tools = Counter("nw_agent_tool_calls_total", "", ["tool", "outcome"], registry=reg)
    terms = Counter("nw_agent_terminations_total", "", ["terminated"], registry=reg)
    drift = Gauge("nw_agent_drift_level", "", registry=reg)
    return reg, {
        "runs": runs,
        "latency": latency,
        "cost": cost,
        "tools": tools,
        "terms": terms,
        "drift": drift,
    }


def record(m: dict, *, n_ok: int = 18, n_err: int = 2, slow: float = 30.0) -> None:
    for _ in range(n_ok):
        m["runs"].labels(role="resolver", terminated="answer").inc()
        m["terms"].labels(terminated="answer").inc()
        m["latency"].labels(role="resolver").observe(1.5)
    for _ in range(n_err):
        m["runs"].labels(role="resolver", terminated="error").inc()
        m["terms"].labels(terminated="error").inc()
        m["latency"].labels(role="resolver").observe(slow)
    m["cost"].labels(role="resolver").inc(0.42)
    m["tools"].labels(tool="search_policies", outcome="ok").inc(5)
    m["tools"].labels(tool="search_policies", outcome="error").inc(2)
    m["drift"].set(1)


def test_emf_line_validates_and_every_metric_is_a_top_level_number():
    reg, m = agent_registry()
    ex = mx.Exporter("agent", stage="", fmt="emf", registry=reg)
    record(m)
    line = ex.line()
    block = line["_aws"]["CloudWatchMetrics"][0]
    assert block["Namespace"] == "Northwind"
    assert block["Dimensions"] == [["Service", "Stage"]]
    assert line["Service"] == "agent" and line["Stage"] == "default"
    names = [x["Name"] for x in block["Metrics"]]
    assert names, "no metrics in the EMF block"
    for name in names:
        assert isinstance(line[name], int | float), f"{name} is not a top-level number"
    assert line["Requests"] == 20 and line["Errors"] == 2 and line["RunsAnswer"] == 18
    assert line["ToolErrors"] == 2 and line["CostUsd"] == pytest.approx(0.42)
    assert line["DriftLevel"] == 1
    # 18 runs at 1.5 s and two at 30 s: the 19th observation of 20 sits in the (20, 40] bucket.
    assert 20000 <= line["LatencyP95Ms"] <= 40000
    json.dumps(line)  # serialisable as one log line


def test_json_line_has_the_fields_the_log_based_metrics_extract():
    reg, m = agent_registry()
    ex = mx.Exporter("agent", stage="staging", fmt="json", registry=reg)
    record(m)
    line = ex.line()
    assert line["msg"] == "metrics_snapshot" and line["severity"] == "INFO"
    assert line["service"] == "agent" and line["stage"] == "staging"
    for field in ("requests", "errors", "latency_p95_ms", "cost_usd", "drift_level"):
        assert isinstance(line[field], int | float), field
    assert line["requests"] == 20 and line["runs_error"] == 2


def test_counters_are_deltas_and_a_quiet_interval_has_no_p95():
    reg, m = agent_registry()
    ex = mx.Exporter("agent", registry=reg)
    record(m)
    first = ex.snapshot()
    assert first["requests"] == 20 and "latency_p95_ms" in first
    second = ex.snapshot()
    assert second["requests"] == 0 and second["cost_usd"] == 0
    assert "latency_p95_ms" not in second, "no observation, no p95"
    assert second["drift_level"] == 1, "gauges are current values, not deltas"


def test_cardinality_is_bounded_whatever_the_registry_holds():
    reg, m = agent_registry()
    for i in range(50):
        m["tools"].labels(tool=f"tool_{i}", outcome="error").inc()
        m["runs"].labels(role=f"role_{i}", terminated="answer").inc()
    Counter("nw_agent_something_else_total", "", ["a", "b"], registry=reg).labels("x", "y").inc()
    ex = mx.Exporter("agent", registry=reg)
    fields = ex.line(ex.snapshot() | {"requests": 1})  # a second call after traffic
    numeric = {k for k, v in fields.items() if isinstance(v, int | float) and k != "ts"}
    assert numeric <= set(mx.FIELDS)
    assert len(numeric) <= mx.MAX_FIELDS
    for name in mx.SERIES:
        assert len(mx.Exporter(name, registry=CollectorRegistry()).snapshot()) <= mx.MAX_FIELDS


def test_p95_interpolates_inside_the_bucket():
    # 100 observations spread evenly across (0, 1]: the 95th lands near 0.95 s.
    buckets = {0.5: 50, 1.0: 100, float("inf"): 100}
    assert mx.p95_from_buckets(buckets, 100) == pytest.approx(950, abs=1)
    assert mx.p95_from_buckets({float("inf"): 3}, 3) == 0.0
    assert mx.p95_from_buckets({}, 0) is None


def test_disabled_when_the_interval_is_zero():
    assert mx.start_metrics_export("triage", env={"NW_METRICS_EXPORT_S": "0"}) is None
    assert mx.start_metrics_export("nobody", env={}) is None


async def test_task_emits_a_line_per_interval_and_a_last_one_on_cancel():
    reg, m = agent_registry()
    out = io.StringIO()
    task = mx.start_metrics_export(
        "northwind-agent",
        env={"NW_METRICS_EXPORT_S": "0.05", "NW_METRICS_FORMAT": "emf", "NW_STAGE": "dev"},
        registry=reg,
        stream=out,
    )
    assert task is not None
    record(m)
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert len(lines) >= 2
    assert lines[0]["Stage"] == "dev" and lines[0]["Requests"] == 20
    assert lines[-1]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Northwind"
