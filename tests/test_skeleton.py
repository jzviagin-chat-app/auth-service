"""Skeleton checks: the app starts, health checks answer, dev-only routes stay hidden in prod."""

from httpx import AsyncClient

from auth_service.db.engine import to_async_url


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_public_healthz(client: AsyncClient) -> None:
    r = await client.get("/auth/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "auth-service"


async def test_docs_hidden_by_default(client: AsyncClient) -> None:
    # ENVIRONMENT defaults to "prod": no interactive docs exposed
    assert (await client.get("/auth/docs")).status_code == 404


def test_async_database_url() -> None:
    assert to_async_url("postgresql://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert to_async_url("postgresql+asyncpg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
