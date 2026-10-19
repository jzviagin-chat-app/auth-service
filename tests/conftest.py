"""Shared test fixtures (like jest's setup files)."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from auth_service.config import get_settings
from auth_service.main import create_app

load_dotenv()  # local .env may set TEST_DATABASE_URL (real environment variables win)

# A database the tests may wipe: NEVER the one you develop against.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (see README, 'Everyday commands')"
)


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
