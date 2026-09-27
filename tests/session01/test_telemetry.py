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
