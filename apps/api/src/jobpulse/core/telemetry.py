"""OpenTelemetry tracing setup (OTLP exporter when an endpoint is configured)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from jobpulse.core.config import Settings

OTLP_EXPORT_TIMEOUT_SECONDS = 10


def configure_tracing(settings: Settings, *, service_name: str | None = None) -> TracerProvider | None:
    """Install a global tracer provider. No-op (returns None) without an OTLP endpoint."""
    if not settings.otel_exporter_otlp_endpoint:
        return None
    resource = Resource.create(
        {"service.name": service_name or settings.service_name, "deployment.environment": settings.environment},
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces",
        timeout=OTLP_EXPORT_TIMEOUT_SECONDS,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider
