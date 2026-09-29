"""The tenant and environment on every log line and every metrics line."""

import json
import logging

from prometheus_client import CollectorRegistry

from nw.logging import JsonFormatter, TextFormatter, bind_static_fields, log_fields, static_fields
from nw.metrics_export import Exporter
from nw.serving.identity import bind_identity, identity


def test_identity_defaults_to_solo_and_northwind():
    assert identity({}) == {"tenant": "solo", "environment": "northwind"}
    assert identity({"NW_TENANT": " alice ", "NW_ENVIRONMENT": "nw-dev"}) == {
        "tenant": "alice",
        "environment": "nw-dev",
    }


def test_bound_identity_lands_on_json_and_text_lines():
    try:
        bind_identity({"NW_TENANT": "alice", "NW_ENVIRONMENT": "northwind"})
        record = logging.LogRecord("t", logging.WARNING, __file__, 1, "drift_alert", None, None)
        record.nw = log_fields(text_length_psi=0.4)["nw"]
        line = json.loads(JsonFormatter().format(record))
        assert line["tenant"] == "alice" and line["environment"] == "northwind"
        assert line["text_length_psi"] == 0.4 and line["msg"] == "drift_alert"
        text = TextFormatter().format(record)
        assert '"tenant": "alice"' in text
        assert static_fields()["tenant"] == "alice"
    finally:
        bind_static_fields(tenant=None, environment=None)
    assert "tenant" not in static_fields()


def test_metrics_snapshot_line_carries_tenant_and_environment():
    registry = CollectorRegistry()
    for fmt in ("json", "emf"):
        exporter = Exporter(
            "triage", fmt=fmt, registry=registry, tenant="alice", environment="northwind"
        )
        line = exporter.line({"requests": 3.0})
        assert line["tenant"] == "alice" and line["environment"] == "northwind"
        if fmt == "emf":
            dims = line["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
            assert dims == [["Service", "Stage"]]  # plain fields, never dimensions
