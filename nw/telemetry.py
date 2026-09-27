"""OpenTelemetry tracing for every service, exported natively per track.

One call at startup, `configure_tracing(service_name)`, picks the exporter:

- `OTEL_EXPORTER_OTLP_ENDPOINT` set: OTLP over HTTP to the local collector and Jaeger
  (`make up-observability`).
- `NW_TRACE_EXPORT=xray`: ADOT's SigV4-signing OTLP exporter to the X-Ray OTLP endpoint,
  `https://xray.<region>.amazonaws.com/v1/traces`. Needs Transaction Search enabled in the
  account and `AWSXrayWriteOnlyPolicy` on the role.
- `NW_TRACE_EXPORT=cloudtrace`: the Cloud Trace exporter. Needs `roles/cloudtrace.agent`.
- none of the above: no exporter. Spans are still created, so the code path is always
  exercised and the tests can assert on them with an in-memory exporter.

Spans carry the correlation ID as an attribute, so a log line and a trace can be
joined from either side. The model call and every tool call are spans of their
own, which is what makes a failed agent run readable in a trace viewer.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from nw.logging import correlation_id, get_logger, log_fields

log = get_logger("nw.telemetry")
_configured = False


def _exporter(env: dict[str, str]) -> tuple[SpanExporter | None, str]:
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(), "otlp"
    mode = env.get("NW_TRACE_EXPORT", "")
    if mode == "xray":
        import botocore.session
        from amazon.opentelemetry.distro.exporter.otlp.aws.traces.otlp_aws_span_exporter import (
            OTLPAwsSpanExporter,
        )

        region = env.get("NW_AWS_REGION") or env.get("AWS_REGION") or "us-east-1"
        return (
            OTLPAwsSpanExporter(
                aws_region=region,
                session=botocore.session.get_session(),
                endpoint=f"https://xray.{region}.amazonaws.com/v1/traces",
            ),
            "xray",
        )
    if mode == "cloudtrace":
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        return CloudTraceSpanExporter(project_id=env.get("NW_GCP_PROJECT") or None), "cloudtrace"
    return None, "none"


def configure_tracing(
    service_name: str, *, exporter: SpanExporter | None = None, env: dict[str, str] | None = None
) -> TracerProvider:
    """Idempotent. Tests pass an in-memory exporter; services call it with no arguments."""
    global _configured
    env = env if env is not None else dict(os.environ)
    provider = trace.get_tracer_provider()
    if _configured and isinstance(provider, TracerProvider):
        # The global provider can be set once per process. A second call with an explicit
        # exporter (tests, notebooks) attaches it to the provider that is already there.
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider
    resource = Resource.create({SERVICE_NAME: env.get("OTEL_SERVICE_NAME", service_name)})
    kind = "custom"
    if exporter is None:
        exporter, kind = _exporter(env)
    kwargs: dict[str, Any] = {}
    if kind == "xray":
        # X-Ray needs its own trace-id format and propagation header.
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

        kwargs["id_generator"] = AwsXRayIdGenerator()
        set_global_textmap(AwsXRayPropagator())
    provider = TracerProvider(resource=resource, **kwargs)
    if exporter is not None:
        processor = (
            SimpleSpanProcessor(exporter) if kind == "custom" else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _configured = True
    log.info("tracing configured", extra=log_fields(service=service_name, exporter=kind))
    return provider


def instrument_app(app: Any) -> None:
    """FastAPI request spans and outbound httpx spans (the agent calling its specialists)."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    HTTPXClientInstrumentor().instrument()
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        # Lambda freezes the sandbox as soon as the response is out, so the batch exporter's
        # thread never gets to run; flush on the way out instead.
        @app.middleware("http")
        async def flush_spans(request: Any, call_next: Any) -> Any:
            response = await call_next(request)
            provider = trace.get_tracer_provider()
            if isinstance(provider, TracerProvider):
                provider.force_flush(timeout_millis=2000)
            return response


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """A span with the correlation ID and any scalar attributes attached."""
    with tracer("nw").start_as_current_span(name) as s:
        cid = correlation_id()
        if cid:
            s.set_attribute("nw.correlation_id", cid)
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, str | bool | int | float) else str(v))
        yield s
