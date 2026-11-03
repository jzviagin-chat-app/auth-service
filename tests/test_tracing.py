"""Tracing (core/tracing.py): spans for requests, queries and Redis commands, trace_id on log
lines, and no phone number, code or token in any span. Spans go to memory instead of Alloy."""

import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auth_service.config import Settings, get_settings
from auth_service.core import tracing
from auth_service.core.logging import JsonFormatter
from auth_service.main import create_app
from tests.conftest import needs_db, needs_redis
from tests.test_devices import bearer
from tests.test_otp_verify import CODE, PHONE, add_code, http, register, verify

pytestmark = [needs_db, needs_redis]


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Makes create_app() trace into memory (as if OTEL_EXPORTER_OTLP_ENDPOINT were set)."""
    spans = InMemorySpanExporter()

    def make_provider(settings: Settings) -> TracerProvider:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(spans))  # exports at once, no batching
        return provider

    monkeypatch.setattr(tracing, "make_tracer_provider", make_provider)
    return spans


@pytest.fixture
async def traced(exporter: InMemorySpanExporter, app: FastAPI) -> AsyncIterator[FastAPI]:
    exporter.clear()  # the fixture's own setup (TRUNCATE, FLUSHDB) was traced too
    yield app
    FastAPIInstrumentor.uninstrument_app(app)


class Lines(logging.Handler):
    """Formats each record when it is logged (inside the request), as the real handler does."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter("auth-service", "test"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def lines() -> Iterator[Lines]:
    handler = Lines()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)


def server_span(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    [span] = [s for s in spans if s.name == name]
    return span


def trace_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return f"{span.context.trace_id:032x}"


async def test_a_request_is_one_trace_with_query_and_redis_spans(
    traced: FastAPI, exporter: InMemorySpanExporter
) -> None:
    async with http(traced) as c:
        r = await c.post("/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 202

    spans = exporter.get_finished_spans()
    root = server_span(spans, "POST /auth/otp/request")
    assert root.attributes is not None and root.attributes["http.status_code"] == 202
    # The request's trace (the resync loop's round, if it ran meanwhile, is a trace of its own).
    children = [s for s in spans if s is not root and trace_id(s) == trace_id(root)]
    systems = {(s.attributes or {}).get("db.system") for s in children}
    assert {"postgresql", "redis"} <= systems


async def test_health_checks_are_not_traced(
    traced: FastAPI, exporter: InMemorySpanExporter
) -> None:
    async with http(traced) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/auth/healthz")).status_code == 200
    assert exporter.get_finished_spans() == ()


async def test_log_lines_carry_the_trace_id(
    traced: FastAPI, exporter: InMemorySpanExporter, lines: Lines
) -> None:
    async with http(traced) as c:
        await c.post("/auth/otp/request", json={"phone": PHONE})
    logging.getLogger("test").info("outside any request")

    root = server_span(exporter.get_finished_spans(), "POST /auth/otp/request")
    [inside] = [line for line in lines.lines if line["msg"] == "otp requested"]
    assert inside["trace_id"] == trace_id(root)
    assert re.fullmatch(r"[0-9a-f]{16}", inside["span_id"])
    [outside] = [line for line in lines.lines if line["msg"] == "outside any request"]
    assert "trace_id" not in outside


async def test_spans_never_contain_the_phone_codes_or_tokens(
    traced: FastAPI, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    send = traced.state.sms.send

    async def recording_send(phone: str, text: str) -> str:
        sent.append(text)
        return await send(phone, text)

    monkeypatch.setattr(traced.state.sms, "send", recording_send)

    async with http(traced) as c:
        await c.post("/auth/otp/request", json={"phone": PHONE})
    [sms_code] = re.findall(r"\d{6}", sent[0])
    await add_code(traced)  # a known code, so verify gets further than "invalid_code"
    status, reply = await verify(traced)
    assert status == 200
    registration_token = reply["registration_token"]
    status, tokens = await register(traced, registration_token)
    assert status == 201
    async with http(traced) as c:
        r = await c.post("/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        new_tokens = r.json()
        r = await c.post("/auth/logout", headers=bearer(new_tokens["access_token"]))
        assert r.status_code == 204
    await traced.state.revoked.drain()  # the background Redis write is traced too

    spans = exporter.get_finished_spans()
    assert len(spans) > 10
    dumped = "\n".join(
        f"{s.name} {json.dumps(dict(s.attributes or {}), default=str)}"
        + "".join(f" {e.name} {dict(e.attributes or {})}" for e in s.events)
        for s in spans
    )
    secrets = [
        PHONE.removeprefix("+"),
        sms_code,
        CODE,
        registration_token,
        tokens["access_token"],
        tokens["refresh_token"],
        new_tokens["access_token"],
        new_tokens["refresh_token"],
    ]
    for secret in secrets:
        assert secret not in dumped


def test_tracing_is_off_without_an_endpoint(prod_settings: None) -> None:
    app = create_app()
    assert app.state.tracer_provider is None
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


async def test_endpoint_turns_tracing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    get_settings.cache_clear()
    try:
        provider = tracing.make_tracer_provider(get_settings())
    finally:
        get_settings.cache_clear()
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "auth-service"
    await tracing.shutdown(provider)  # nothing was exported, so this returns at once
