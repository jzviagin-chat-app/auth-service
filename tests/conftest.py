"""Shared test fixtures (like jest's setup files)."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from auth_service.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process (no server, no network)."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
