"""Spans exist for the model call with token and cost attributes, carrying the correlation ID."""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nw import telemetry
from nw.llm.providers.fake import FakeProvider
from nw.logging import bind_correlation_id

pytestmark = pytest.mark.session01


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter=exporter, env={})
    yield exporter
    exporter.clear()


async def test_completion_span_has_model_tokens_cost_and_correlation(spans, make_client):
    client = make_client(FakeProvider(tokens_per_call=(100, 10)))
    with bind_correlation_id("trace-me"):
        await client.complete("hello")
    telemetry.trace.get_tracer_provider().force_flush()
    names = [s.name for s in spans.get_finished_spans()]
    assert "llm.complete" in names
    s = next(x for x in spans.get_finished_spans() if x.name == "llm.complete")
    a = dict(s.attributes)
    assert a["gen_ai.request.model"] == "fake-workhorse"
    assert a["gen_ai.usage.input_tokens"] == 100 and a["gen_ai.usage.output_tokens"] == 10
    assert a["nw.cost_usd"] > 0 and a["nw.correlation_id"] == "trace-me"


def test_no_exporter_means_no_crash(monkeypatch):
    telemetry.configure_tracing("test", env={})
    with telemetry.span("anything", k="v"):
        pass


def test_azure_track_picks_the_azure_monitor_exporter_without_sending(monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExporter

    made: list[str] = []

    class Sentinel(SpanExporter):
        def export(self, spans):  # pragma: no cover - never called
            raise AssertionError("nothing is sent")

    def fake(connection_string):
        made.append(connection_string)
        return Sentinel()

    monkeypatch.setattr(telemetry, "azure_monitor_exporter", fake)
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    exporter, kind = telemetry._exporter(
        {"NW_TRACK": "azure", "APPLICATIONINSIGHTS_CONNECTION_STRING": conn}
    )
    assert kind == "azuremonitor" and isinstance(exporter, Sentinel) and made == [conn]
    _, kind = telemetry._exporter(
        {"NW_TRACK": "azure", "NW_AZURE_APPINSIGHTS_CONNECTION_STRING": conn}
    )
    assert kind == "azuremonitor"
    # other tracks, no connection string, or an explicit OTLP collector: not Azure Monitor
    assert (
        telemetry._exporter({"NW_TRACK": "aws", "APPLICATIONINSIGHTS_CONNECTION_STRING": conn})[1]
        == "none"
    )
    assert telemetry._exporter({"NW_TRACK": "azure"})[1] == "none"
    assert (
        telemetry._exporter(
            {"NW_TRACE_EXPORT": "azuremonitor", "NW_AZURE_APPINSIGHTS_CONNECTION_STRING": conn}
        )[1]
        == "azuremonitor"
    )


def test_azure_monitor_import_failure_degrades_to_no_exporter(monkeypatch):
    def broken(connection_string):
        raise ImportError("cannot import name 'LogData'")

    monkeypatch.setattr(telemetry, "azure_monitor_exporter", broken)
    exporter, kind = telemetry._exporter(
        {"NW_TRACK": "azure", "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=x"}
    )
    assert (exporter, kind) == (None, "none")
