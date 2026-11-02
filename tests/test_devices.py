"""The logged-in caller (CurrentDevice), POST /auth/logout, GET/DELETE /auth/devices, and revoked
devices published in Redis for chat-service (services/revocation.py). Real Postgres + Redis."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import update

from auth_service.core.logging import JsonFormatter
from auth_service.db import repos
from auth_service.db.models import Device, User
from auth_service.services.revocation import MARKER, device_key, revoke_device
from auth_service.services.tokens import issue_access_token
from tests.conftest import needs_db, needs_redis
from tests.test_otp_verify import add_code, http, ids, rows, verify
from tests.test_refresh import logged_in, refresh

pytestmark = [needs_db, needs_redis]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def call(app: FastAPI, method: str, path: str, token: str | None = None) -> tuple[int, Any]:
    async with http(app) as c:
        r = await c.request(method, path, headers=bearer(token) if token else {})
    return r.status_code, (r.json() if r.content else None)


async def is_published(app: FastAPI, device_id: str) -> bool:
    await app.state.revoked.drain()  # the write runs in the background, after the commit
    return bool(await app.state.revoked.redis.exists(device_key(uuid.UUID(device_id))))


async def add_linked_device(app: FastAPI, user_id: str) -> tuple[str, str]:
    """A linked device by hand (QR linking is step 10); returns (device id, its access token)."""
    async with app.state.db.session() as s:
        device = await repos.insert_device(
            s,
            user_id=uuid.UUID(user_id),
            platform="web",
            name="browser",
            role="linked",
            now=datetime.now(UTC),
        )
        await s.commit()
        device_id = device.id
    token, _ = issue_access_token(
        app.state.keys,
        user_id=uuid.UUID(user_id),
        device_id=device_id,
        issuer="auth-service",
        ttl_seconds=900,
    )
    return str(device_id), token


# ---- CurrentDevice ----


async def test_no_token(app: FastAPI) -> None:
    async with http(app) as c:
        r = await c.get("/auth/devices")
    assert (r.status_code, r.json()["error"]) == (401, "invalid_token")
    assert r.headers["www-authenticate"] == "Bearer"


async def test_bad_and_expired_tokens(app: FastAPI) -> None:
    body = await logged_in(app)
    user_id, device_id = ids(app, body)
    expired, _ = issue_access_token(
        app.state.keys,
        user_id=uuid.UUID(user_id),
        device_id=uuid.UUID(device_id),
        issuer="auth-service",
        ttl_seconds=900,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    for token in ("garbage", expired, body["access_token"][:-4] + "AAAA"):
        status, err = await call(app, "GET", "/auth/devices", token)
        assert (status, err["error"]) == (401, "invalid_token")


async def test_blocked_account(app: FastAPI) -> None:
    body = await logged_in(app)
    async with app.state.db.session() as s:
        await s.execute(update(User).values(status="blocked"))
        await s.commit()
    status, err = await call(app, "GET", "/auth/devices", body["access_token"])
    assert (status, err["error"]) == (403, "account_blocked")


# ---- GET /auth/devices ----


async def test_list_devices(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    status, out = await call(app, "GET", "/auth/devices", body["access_token"])
    assert status == 200
    [me] = out["devices"]
    assert (me["id"], me["platform"], me["name"]) == (device_id, "ios", "test phone")
    assert (me["role"], me["current"]) == ("primary", True)
    assert me["created_at"] and me["last_seen_at"]


async def test_list_shows_only_active_devices(app: FastAPI) -> None:
    body = await logged_in(app)
    user_id, _ = ids(app, body)
    linked_id, _ = await add_linked_device(app, user_id)
    gone_id, _ = await add_linked_device(app, user_id)
    assert (await call(app, "DELETE", f"/auth/devices/{gone_id}", body["access_token"]))[0] == 204
    _, out = await call(app, "GET", "/auth/devices", body["access_token"])
    assert [(d["role"], d["current"]) for d in out["devices"]] == [
        ("primary", True),
        ("linked", False),
    ]
    assert out["devices"][1]["id"] == linked_id


async def test_linked_device_cannot_list_or_remove(app: FastAPI) -> None:
    body = await logged_in(app)
    user_id, primary_id = ids(app, body)
    _, linked_token = await add_linked_device(app, user_id)
    status, err = await call(app, "GET", "/auth/devices", linked_token)
    assert (status, err["error"]) == (403, "action_not_allowed")
    status, err = await call(app, "DELETE", f"/auth/devices/{primary_id}", linked_token)
    assert (status, err["error"]) == (403, "action_not_allowed")


# ---- POST /auth/logout ----


async def test_logout(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    assert (await call(app, "POST", "/auth/logout", body["access_token"]))[0] == 204

    [device] = await rows(app, Device)
    assert device.revoked_at is not None
    assert (await call(app, "GET", "/auth/devices", body["access_token"]))[0] == 401
    assert (await refresh(app, body["refresh_token"]))[0] == 401
    assert await is_published(app, device_id)
    ttl = await app.state.revoked.redis.pttl(device_key(uuid.UUID(device_id)))
    assert 1_000_000 < ttl <= 1_020_000  # ~17 minutes

    # Again, with the same (now signed-out) token: still 204.
    assert (await call(app, "POST", "/auth/logout", body["access_token"]))[0] == 204


async def test_logout_needs_a_valid_token(app: FastAPI) -> None:
    status, err = await call(app, "POST", "/auth/logout", "garbage")
    assert (status, err["error"]) == (401, "invalid_token")


# ---- DELETE /auth/devices/{id} ----


async def test_primary_removes_a_linked_device(app: FastAPI) -> None:
    body = await logged_in(app)
    user_id, _ = ids(app, body)
    linked_id, linked_token = await add_linked_device(app, user_id)
    path = f"/auth/devices/{linked_id}"
    assert (await call(app, "DELETE", path, body["access_token"]))[0] == 204
    assert await is_published(app, linked_id)
    assert (await call(app, "POST", "/auth/logout", linked_token))[0] == 204  # still fine
    assert (await call(app, "DELETE", path, body["access_token"]))[0] == 204  # again: 204


async def test_remove_refusals(app: FastAPI) -> None:
    body = await logged_in(app)
    _, me = ids(app, body)
    token = body["access_token"]

    status, err = await call(app, "DELETE", f"/auth/devices/{me}", token)
    assert (status, err["error"]) == (403, "action_not_allowed")  # itself: log out instead

    status, err = await call(app, "DELETE", f"/auth/devices/{uuid.uuid7()}", token)
    assert (status, err["error"]) == (404, "not_found")

    # Another user's device looks exactly like an unknown one.
    other = await other_users_device(app)
    status, err = await call(app, "DELETE", f"/auth/devices/{other}", token)
    assert (status, err["error"]) == (404, "not_found")


async def other_users_device(app: FastAPI) -> str:
    async with app.state.db.session() as s:
        user = await repos.insert_user(s, phone_e164="+972501112222", display_name="Other")
        device = await repos.insert_device(
            s, user_id=user.id, platform="ios", name=None, role="primary", now=datetime.now(UTC)
        )
        await s.commit()
        return str(device.id)


# ---- every revocation is published for chat-service ----


async def test_new_sms_login_publishes_the_replaced_phone(app: FastAPI) -> None:
    first = await logged_in(app)
    await add_code(app, "654321")
    status, _ = await verify(app, "654321")
    assert status == 200
    _, old_device = ids(app, first)
    assert await is_published(app, old_device)
    assert (await call(app, "GET", "/auth/devices", first["access_token"]))[0] == 401


async def test_refresh_reuse_publishes_the_device(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    a = body["refresh_token"]
    b = (await refresh(app, a))[1]["refresh_token"]
    await refresh(app, b)
    assert (await refresh(app, a))[0] == 401  # reuse: the device is signed out
    assert await is_published(app, device_id)


async def test_redis_failure_still_logs_out_and_resync_publishes(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    body = await logged_in(app)
    _, device_id = ids(app, body)
    redis = app.state.revoked.redis
    real_pipeline = redis.pipeline
    calls = 0

    class BrokenPipeline:
        def set(self, *args: Any, **kwargs: Any) -> None: ...

        async def execute(self) -> None:
            nonlocal calls
            calls += 1
            raise RedisConnectionError("down")

        async def __aenter__(self) -> BrokenPipeline:
            return self

        async def __aexit__(self, *args: object) -> None: ...

    monkeypatch.setattr(redis, "pipeline", lambda **kwargs: BrokenPipeline())
    assert (await call(app, "POST", "/auth/logout", body["access_token"]))[0] == 204
    assert not await is_published(app, device_id)  # (drains the background write first)
    assert calls == 3
    assert "revoked device not written to redis; the resync will" in caplog.text

    monkeypatch.setattr(redis, "pipeline", real_pipeline)
    assert await app.state.revoked.resync(app.state.db) == 1
    assert await is_published(app, device_id)
    assert await redis.exists(MARKER)


async def test_rolled_back_revocation_is_not_published(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    async with app.state.db.session() as s:
        assert await revoke_device(
            s, app.state.revoked, uuid.UUID(device_id), datetime.now(UTC), "test"
        )
        await s.rollback()
        await s.commit()  # a later commit on the same session must not publish it either
    assert not await is_published(app, device_id)
    [device] = await rows(app, Device)
    assert device.revoked_at is None


async def test_savepoint_does_not_publish_early(app: FastAPI) -> None:
    # Releasing a savepoint isn't a commit: nothing may reach Redis until the real commit.
    body = await logged_in(app)
    _, device_id = ids(app, body)
    async with app.state.db.session() as s:
        await revoke_device(s, app.state.revoked, uuid.UUID(device_id), datetime.now(UTC), "test")
        async with s.begin_nested():
            pass
        assert not await is_published(app, device_id)
        await s.commit()
    assert await is_published(app, device_id)


async def test_resync_refills_redis_after_a_restart(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    await call(app, "POST", "/auth/logout", body["access_token"])
    redis = app.state.revoked.redis
    await redis.flushdb()  # what a Redis restart looks like: everything gone, marker too
    assert not await redis.exists(MARKER)

    await app.state.revoked.resync(app.state.db)
    assert await is_published(app, device_id)
    assert 0 < await redis.ttl(MARKER) <= 180


async def test_resync_skips_old_revocations(app: FastAPI) -> None:
    body = await logged_in(app)
    _, device_id = ids(app, body)
    async with app.state.db.session() as s:
        await s.execute(update(Device).values(revoked_at=datetime.now(UTC) - timedelta(minutes=30)))
        await s.commit()
    assert await app.state.revoked.resync(app.state.db) == 0  # its tokens expired long ago
    assert not await is_published(app, device_id)


async def test_logs_never_contain_tokens(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)
    body = await logged_in(app)
    user_id, _ = ids(app, body)
    linked_id, _ = await add_linked_device(app, user_id)
    await call(app, "GET", "/auth/devices", body["access_token"])
    await call(app, "DELETE", f"/auth/devices/{linked_id}", body["access_token"])
    await call(app, "POST", "/auth/logout", body["access_token"])
    output = "\n".join(JsonFormatter("auth-service", "test").format(r) for r in caplog.records)
    assert "device signed out" in output
    assert '"reason": "logout"' in output and '"reason": "removed_by_primary"' in output
    for secret in (body["access_token"], body["refresh_token"]):
        assert secret not in output
