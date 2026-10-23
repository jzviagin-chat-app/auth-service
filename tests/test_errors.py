"""Every error has the same shape: {"error": "<machine_code>", "message": "<human text>"}."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from auth_service.config import get_settings
from auth_service.db.engine import Database
from auth_service.main import create_app
from auth_service.services.sms import FakeSmsSender


@pytest.fixture
async def configured(
    prod_settings: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """An app whose database and Redis are configured but never reached: both connect lazily, and
    the request fails validation before any query (FastAPI resolves dependencies first)."""
    monkeypatch.setenv("OTP_PEPPER", "test-pepper")
    get_settings.cache_clear()
    app = create_app()
    app.state.db = Database("postgresql://nobody@localhost:1/none", 1, 0)
    app.state.redis = Redis.from_url("redis://localhost:1/0")
    app.state.sms = FakeSmsSender(show_text=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.redis.aclose()
    await app.state.db.dispose()


async def test_not_found(client: AsyncClient) -> None:
    r = await client.get("/auth/nope")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "message": "Not Found"}


async def test_method_not_allowed(client: AsyncClient) -> None:
    r = await client.get("/auth/otp/request")
    assert r.status_code == 405
    assert r.json()["error"] == "method_not_allowed"


async def test_validation_error_names_fields_but_never_echoes_values(
    configured: AsyncClient,
) -> None:
    r = await configured.post("/auth/otp/request", json={"phone": "+972501234567" * 3})  # too long
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request", "message": "Invalid or missing fields: phone"}
    assert "972501234567" not in r.text


async def test_missing_field(configured: AsyncClient) -> None:
    r = await configured.post("/auth/otp/request", json={})
    assert r.status_code == 422
    assert r.json()["message"] == "Invalid or missing fields: phone"


async def test_unexpected_error(prod_settings: None) -> None:
    app = create_app()

    @app.get("/test/boom")
    async def boom() -> None:
        raise RuntimeError("bug")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/test/boom")
    assert r.status_code == 500
    assert r.json()["error"] == "internal_error"
    assert "bug" not in r.text  # the exception's text stays in the logs, not the response


async def test_service_not_configured(client: AsyncClient) -> None:
    # The `client` fixture doesn't start the app's lifespan: no database, no Redis -> 503.
    r = await client.post("/auth/otp/request", json={"phone": "+972501234567"})
    assert r.status_code == 503
    assert set(r.json()) == {"error", "message"}
