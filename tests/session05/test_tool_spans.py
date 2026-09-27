import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nw import telemetry
from nw.agent.loop import run_agent, scripted_completion
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session05


async def test_run_and_tool_spans(registry, make_client):
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter=exporter, env={})
    provider = FakeProvider(
        [
            scripted_completion("", [("lookup_customer", {"account_id": "NW-10000"})]),
            scripted_completion("done"),
        ]
    )
    t = await run_agent("Ticket from NW-10000", registry, make_client(provider))
    telemetry.trace.get_tracer_provider().force_flush()
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert (
        "agent.run" in by_name and "tool.lookup_customer" in by_name and "llm.complete" in by_name
    )
    assert by_name["agent.run"].attributes["nw.run_id"] == t.run_id
    assert by_name["tool.lookup_customer"].attributes["nw.ok"] is True
    assert by_name["tool.lookup_customer"].parent.span_id == by_name["agent.run"].context.span_id
