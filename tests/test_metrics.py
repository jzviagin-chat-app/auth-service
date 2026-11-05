"""Metrics (core/metrics.py): GET /metrics, request counts by route template, the auth counters,
and no phone number, code or token in what Prometheus reads.

Counters live for the whole test process (like in a pod), so each test compares before and after.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from prometheus_client import REGISTRY

from auth_service.core.metrics import route_template
from auth_service.services.revocation import revoke_device
from tests.conftest import needs_db, needs_redis
from tests.test_otp_verify import PHONE, add_code, http, ids, register, verify
from tests.test_refresh import logged_in, ok, rejected

pytestmark = [needs_db, needs_redis]


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def metrics_text(app: FastAPI) -> str:
    async with http(app) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


def test_route_template_ignores_the_values() -> None:
    def template(path: str, route_path: str | None) -> str:
        route = SimpleNamespace(path=route_path) if route_path else None
        return route_template({"type": "http", "path": path, "route": route})

    # Two parameters with the same value, and a value equal to a fixed piece of the URL.
    two = "/users/{user_id}/devices/{device_id}"
    assert template("/auth/users/x/devices/x", two) == f"/auth{two}"
    assert template("/auth/devices/devices", "/devices/{device_id}") == "/auth/devices/{device_id}"
    assert template("/auth/otp/request", "/auth/otp/request") == "/auth/otp/request"  # full path
    assert template("/nothing/here", None) == "unmatched"


async def test_metrics_are_served_at_the_root_only(app: FastAPI) -> None:
    assert "http_requests_total" in await metrics_text(app)
    async with http(app) as c:
        # /auth/* is what the ingress sends here: /metrics must not be reachable from outside.
        assert (await c.get("/auth/metrics")).status_code == 404


async def test_requests_are_counted_by_route_template(app: FastAPI) -> None:
    route = "/auth/devices/{device_id}"
    before = value("http_requests_total", method="DELETE", route=route, status="401")
    unmatched = value("http_requests_total", method="GET", route="unmatched", status="404")
    health = value("http_requests_total", method="GET", route="/healthz", status="200")
    async with http(app) as c:
        assert (await c.delete(f"/auth/devices/{uuid.uuid4()}")).status_code == 401
        assert (await c.get(f"/nothing/{uuid.uuid4()}")).status_code == 404
        assert (await c.get("/healthz")).status_code == 200

    assert value("http_requests_total", method="DELETE", route=route, status="401") == before + 1
    assert value("http_request_duration_seconds_count", method="DELETE", route=route) > 0
    # Unknown URLs share one series, and the probes aren't counted.
    assert value("http_requests_total", method="GET", route="unmatched", status="404") == (
        unmatched + 1
    )
    assert value("http_requests_total", method="GET", route="/healthz", status="200") == health


async def test_sign_up_is_counted_and_never_exposed(app: FastAPI) -> None:
    sent = value("auth_otp_requests_total", outcome="sent")
    sms_ok = value("auth_sms_sent_total", provider="fake", result="ok")
    required = value("auth_logins_total", outcome="registration_required")
    registered = value("auth_logins_total", outcome="registered")

    async with http(app) as c:
        assert (await c.post("/auth/otp/request", json={"phone": PHONE})).status_code == 202
    await add_code(app)
    status, reply = await verify(app)
    assert status == 200
    status, tokens = await register(app, reply["registration_token"])
    assert status == 201

    assert value("auth_otp_requests_total", outcome="sent") == sent + 1
    assert value("auth_sms_sent_total", provider="fake", result="ok") == sms_ok + 1
    assert value("auth_logins_total", outcome="registration_required") == required + 1
    assert value("auth_logins_total", outcome="registered") == registered + 1

    text = await metrics_text(app)
    for secret in (
        PHONE.removeprefix("+"),
        reply["registration_token"],
        tokens["access_token"],
        tokens["refresh_token"],
    ):
        assert secret not in text


async def test_refresh_reuse_is_counted(app: FastAPI) -> None:
    rotated = value("auth_refresh_total", outcome="rotated")
    reused = value("auth_refresh_total", outcome="reused")
    revoked = value("auth_devices_revoked_total", reason="refresh_token_reused")

    a = (await logged_in(app))["refresh_token"]
    await ok(app, await ok(app, a))
    await rejected(app, a)  # a copy of a replaced token: the device is signed out

    assert value("auth_refresh_total", outcome="rotated") == rotated + 2
    assert value("auth_refresh_total", outcome="reused") == reused + 1
    assert value("auth_devices_revoked_total", reason="refresh_token_reused") == revoked + 1


async def test_a_rolled_back_revocation_is_not_counted(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    before = value("auth_devices_revoked_total", reason="test")
    async with app.state.db.session() as session:
        assert await revoke_device(
            session, app.state.revoked, uuid.UUID(device_id), datetime.now(UTC), "test"
        )
        await session.rollback()
    assert value("auth_devices_revoked_total", reason="test") == before
