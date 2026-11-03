"""Tracing: OpenTelemetry spans, sent over OTLP to Alloy, which forwards them to Tempo.

A trace is the timeline of one request: a span for the HTTP request, and inside it one span per
Postgres query, Redis command and outgoing HTTP call (Global SMS). We don't create these spans by
hand: one ready-made "instrumentation" package per library hooks into it (like
@opentelemetry/auto-instrumentations-node):
  FastAPI     an ASGI middleware around the app (instrument_app, in create_app)
  SQLAlchemy  its event system (before/after each query) on our engine
  redis       wraps redis-py's execute_command / pipeline execute (monkey-patching)
  httpx       wraps httpx's transport (monkey-patching)
The current span lives in a contextvar (≈ AsyncLocalStorage), so every query made while handling a
request becomes a child of that request's span. core/logging.py puts its trace_id on log lines.

Off when OTEL_EXPORTER_OTLP_ENDPOINT is unset (local, CI): no provider, nothing patched.

Privacy (never phone numbers, codes or tokens in a span): SQLAlchemy records the SQL text with
placeholders, never the values; redis records only the command name ("SET ? ?"); httpx records
the URL (Global SMS gets its key in the body); FastAPI records method, route and status, never
bodies. tests/test_tracing.py checks this.
"""

import asyncio

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from auth_service.config import Settings

# Kubernetes probes call these every few seconds: no traces for them (regexes on the URL).
EXCLUDED_URLS = "/healthz$"

# For our own spans, where no library makes one: the background loops (cleanup, revoked-devices
# resync), so each round is one trace instead of a loose trace per query. It forwards to the
# provider registered in instrument_libraries(); with tracing off, its spans do nothing.
tracer = trace.get_tracer("auth_service")


def make_tracer_provider(settings: Settings) -> TracerProvider | None:
    """The provider that collects spans and exports them in batches (a background thread, so a
    slow or dead Alloy never delays a request; spans it can't send are dropped). None = off."""
    if not settings.otel_exporter_otlp_endpoint:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "k8s.pod.name": settings.pod_name,
                "deployment.environment.name": settings.environment,
            }
        )
    )
    # http:// = plain gRPC (inside the cluster), as the exporter decides from the scheme.
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def instrument_app(app: FastAPI, provider: TracerProvider) -> None:
    """A span per HTTP request. Must run in create_app(): the middleware stack is built before
    the lifespan starts. `exclude_spans` drops the per-chunk receive/send spans (noise)."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=EXCLUDED_URLS,
        exclude_spans=["receive", "send"],
    )


def instrument_libraries(provider: TracerProvider, engine: AsyncEngine | None) -> None:
    """Spans for Redis commands, outgoing HTTP calls and our engine's queries. Redis and httpx
    are patched for the whole process; call before creating clients (main.lifespan). Also
    registers the provider globally, for `tracer` above (allowed once per process)."""
    trace.set_tracer_provider(provider)
    RedisInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    if engine is not None:
        # AsyncEngine runs a plain (sync) Engine underneath; that is where the events fire.
        # skip_dep_check: release 0.66b2 still says "sqlalchemy < 2.1" and would do nothing with
        # our 2.1; its main branch already allows < 2.2. Drop this once a release has that
        # (test_a_request_is_one_trace_with_query_and_redis_spans checks the query spans).
        SQLAlchemyInstrumentor().instrument(
            engine=engine.sync_engine, tracer_provider=provider, skip_dep_check=True
        )


async def shutdown(provider: TracerProvider) -> None:
    """Undo the patches and send the spans still waiting. Flushing blocks (it waits for the
    exporter), so it runs on a worker thread, never on the event loop."""
    for instrumentor in (RedisInstrumentor(), HTTPXClientInstrumentor(), SQLAlchemyInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    await asyncio.to_thread(provider.shutdown)
