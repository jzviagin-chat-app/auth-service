"""The client IP the app sees (request.client.host), behind the HAProxy ingress.

HAProxy *appends* the real client IP to whatever X-Forwarded-For the client sent, so the leftmost
entry can be faked. Only the ingress (inside the pod network 10.42.0.0/16) is trusted, and the app
takes the rightmost address that isn't a trusted proxy.
"""

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from auth_service.main import create_app

INGRESS = "10.42.0.22"  # the HAProxy controller pod
REAL = "147.235.199.4"


@pytest.fixture
def app(prod_settings: None) -> FastAPI:
    app = create_app()

    @app.get("/test/ip")
    async def ip(request: Request) -> dict[str, str | None]:
        return {"ip": request.client.host if request.client else None}

    return app


async def seen_ip(app: FastAPI, peer: str, forwarded_for: str | None) -> str:
    transport = ASGITransport(app=app, client=(peer, 1234))
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/test/ip", headers=headers)
    ip: str = r.json()["ip"]
    return ip


async def test_from_ingress(app: FastAPI) -> None:
    assert await seen_ip(app, INGRESS, REAL) == REAL


async def test_faked_entry_is_ignored(app: FastAPI) -> None:
    # The client sent "X-Forwarded-For: 6.6.6.6"; HAProxy appended the real address.
    assert await seen_ip(app, INGRESS, f"6.6.6.6, {REAL}") == REAL


async def test_untrusted_peer_cannot_set_its_ip(app: FastAPI) -> None:
    # Not from the ingress (e.g. another pod): its X-Forwarded-For is not believed.
    assert await seen_ip(app, "203.0.113.9", "6.6.6.6") == "203.0.113.9"


async def test_no_header(app: FastAPI) -> None:
    assert await seen_ip(app, INGRESS, None) == INGRESS
