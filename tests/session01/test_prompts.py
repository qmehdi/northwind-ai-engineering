"""Prompts are versioned: a name and a content hash that travel on every model-call span."""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from nw import telemetry
from nw.llm import prompts
from nw.llm.prompts import Prompt, prompt_hash, register, version_for
from nw.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.session01


def test_hash_is_twelve_hex_chars_and_changes_with_the_text():
    a, b = prompt_hash("You answer policy questions."), prompt_hash("You answer policy questions!")
    assert len(a) == 12 and int(a, 16) >= 0
    assert a != b
    p = Prompt("t.one", "You answer policy questions.")
    assert p.version == f"t.one@{a}" and str(p) == p.text


def test_register_is_idempotent_for_identical_text_and_refuses_a_silent_change():
    p = register("test.idempotent", "same text")
    assert register("test.idempotent", "same text") == p
    with pytest.raises(ValueError, match="already registered"):
        register("test.idempotent", "different text")
    assert prompts.get("test.idempotent").text == "same text"
    assert "test.idempotent" in prompts.versions()


def test_version_for_names_registered_text_and_flags_the_rest():
    register("test.lookup", "look me up")
    assert version_for("look me up") == f"test.lookup@{prompt_hash('look me up')}"
    assert version_for("never registered").startswith("unregistered@")
    assert version_for(None) is None


def test_cli_lists_the_course_prompts(capsys):
    assert prompts.main([]) == 0
    out = capsys.readouterr().out
    assert "| policy.answer |" in out and "| policy.judge |" in out and "| llm.structured |" in out


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    telemetry.configure_tracing("test", exporter=exporter, env={})
    yield exporter
    exporter.clear()


def _span_attr(exporter, name):
    telemetry.trace.get_tracer_provider().force_flush()
    s = [x for x in exporter.get_finished_spans() if x.name == "llm.complete"][-1]
    return dict(s.attributes).get(name)


async def test_complete_span_carries_the_prompt_version(spans, make_client):
    p = register("test.span", "You are a test prompt.")
    client = make_client(FakeProvider())
    await client.complete("hello", system=p.text)
    assert _span_attr(spans, "nw.prompt_version") == p.version
    await client.complete("hello", system="ad hoc text")
    assert _span_attr(spans, "nw.prompt_version").startswith("unregistered@")
    await client.complete("hello")
    assert _span_attr(spans, "nw.prompt_version") is None


async def test_structured_span_names_the_callers_prompt_not_the_wrapper(spans, make_client):
    class Out(BaseModel):
        ok: bool

    p = register("test.structured", "Decide whether it is ok.")
    provider = FakeProvider(['{"ok": true}'])
    client = make_client(provider)
    out = await client.structured("is it?", Out, system=p.text)
    assert out.ok
    assert _span_attr(spans, "nw.prompt_version") == p.version
    # The wrapper text the model saw is the registered structured-output instruction.
    assert prompts.STRUCTURED_INSTRUCTIONS.text in provider.calls[0]["system"]
