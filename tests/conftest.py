"""Shared test fixtures (like jest's setup files)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from auth_service.config import get_settings
from auth_service.main import create_app
from auth_service.services.keys import SigningKeys

load_dotenv()  # local .env may set TEST_DATABASE_URL (real environment variables win)

# A database the tests may wipe: NEVER the one you develop against.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (see README, 'Everyday commands')"
)
# Same for Redis: a db number the tests may wipe (15 locally and in CI).
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(
    not TEST_REDIS_URL, reason="TEST_REDIS_URL not set (see README, 'Everyday commands')"
)


def alembic(*args: str) -> str:
    """Run the alembic CLI exactly like the cluster does, against the test database."""
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL or ""}
    result = subprocess.run(  # noqa: S603 (fixed command, test only)
        [sys.executable, "-m", "alembic", *args],  # the alembic installed in this .venv
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


@pytest.fixture
def prod_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Settings as in production, whatever the local .env says."""
    monkeypatch.setenv("ENVIRONMENT", "prod")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(prod_settings: None) -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process (no server, no network)."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---- the app against the test database (login, register, refresh tests) ----
# A test module that defines its own `app` / `migrated` fixture uses that one instead.

PEPPER = "test-pepper"


@pytest.fixture(scope="module")
def migrated() -> None:
    alembic("upgrade", "head")


@pytest.fixture
async def app(migrated: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    env = {"ENVIRONMENT": "prod", "DATABASE_URL": TEST_DATABASE_URL or "", "OTP_PEPPER": PEPPER}
    if TEST_REDIS_URL:
        env["REDIS_URL"] = TEST_REDIS_URL
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with app.state.db.engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE users, devices, refresh_tokens, otp_challenges, sms_daily_usage")
            )
        if app.state.redis is not None:
            await app.state.redis.flushdb()
        app.state.keys = SigningKeys(Ed25519PrivateKey.generate())
        yield app
    get_settings.cache_clear()
