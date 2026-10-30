"""POST /auth/token/refresh against a real Postgres: rotation, lost replies, replies out of order,
reuse (theft) detection, cleanup. The rules are in services/refresh.py."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import update

from auth_service.core.hashing import hash_secret
from auth_service.core.logging import JsonFormatter
from auth_service.db.models import Device, RefreshToken, User
from auth_service.services.cleanup import delete_old_refresh_tokens
from tests.conftest import needs_db
from tests.test_otp_verify import CODE, add_code, add_user, check_token, http, ids, rows, verify

pytestmark = needs_db


async def logged_in(app: FastAPI) -> dict[str, Any]:
    await add_user(app)
    await add_code(app)
    status, body = await verify(app)
    assert status == 200
    return body


async def refresh(app: FastAPI, token: str) -> tuple[int, dict[str, Any]]:
    async with http(app) as c:
        r = await c.post("/auth/token/refresh", json={"refresh_token": token})
    return r.status_code, r.json()


async def ok(app: FastAPI, token: str) -> str:
    """Refresh successfully; returns the new refresh token."""
    status, body = await refresh(app, token)
    assert status == 200, body
    check_token(app, body)
    return str(body["refresh_token"])


async def rejected(app: FastAPI, token: str) -> None:
    status, body = await refresh(app, token)
    assert (status, body["error"]) == (401, "invalid_refresh_token")


async def device_revoked(app: FastAPI) -> bool:
    [device] = await rows(app, Device)
    return device.revoked_at is not None


async def token_row(app: FastAPI, token: str) -> RefreshToken:
    [row] = [t for t in await rows(app, RefreshToken) if t.token_hash == hash_secret(token)]
    return row


# ---- normal use ----


async def test_login_and_register_return_a_refresh_token(app: FastAPI) -> None:
    body = await logged_in(app)
    [row] = await rows(app, RefreshToken)
    assert str(row.device_id) == ids(app, body)[1]
    assert row.token_hash == hash_secret(body["refresh_token"])  # only the hash is stored
    assert row.expires_at > datetime.now(UTC) + timedelta(days=59)


async def test_rotation(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    b = await ok(app, a)
    c = await ok(app, b)
    assert len({a, b, c}) == 3
    await ok(app, c)
    assert not await device_revoked(app)


async def test_refresh_updates_last_seen(app: FastAPI) -> None:
    body = await logged_in(app)
    async with app.state.db.session() as s:
        await s.execute(update(Device).values(last_seen_at=datetime.now(UTC) - timedelta(days=1)))
        await s.commit()
    await ok(app, body["refresh_token"])
    [device] = await rows(app, Device)
    assert device.last_seen_at is not None
    assert device.last_seen_at > datetime.now(UTC) - timedelta(minutes=1)


# ---- lost replies and replies out of order: nobody is logged out ----


async def test_lost_reply_retry_with_the_same_token(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    await ok(app, a)  # reply lost: the app never sees this token
    b = await ok(app, a)  # retry, maybe hours later
    await ok(app, b)
    assert not await device_revoked(app)


async def test_lost_reply_of_the_first_refresh_after_login(app: FastAPI) -> None:
    # The app never saved the login reply's successor and keeps retrying with the current token.
    a = (await logged_in(app))["refresh_token"]
    b = await ok(app, a)
    await ok(app, b)  # lost
    await ok(app, b)  # lost again
    c = await ok(app, b)
    await ok(app, c)
    assert not await device_revoked(app)


async def test_replies_out_of_order(app: FastAPI) -> None:
    # Three refreshes with the same token; the app keeps whichever reply it saw last.
    a = (await logged_in(app))["refresh_token"]
    b1, b2, b3 = [await ok(app, a) for _ in range(3)]
    await ok(app, b2)  # any of them works; the first used wins
    for loser in (b1, b3):
        assert (await token_row(app, loser)).discarded_at is not None
    assert not await device_revoked(app)


async def test_parallel_refreshes_with_one_token(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    results = await asyncio.gather(*(refresh(app, a) for _ in range(5)))
    assert [status for status, _ in results] == [200] * 5
    await ok(app, results[3][1]["refresh_token"])
    assert not await device_revoked(app)


async def test_waiting_tokens_are_capped(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    retries = [await ok(app, a) for _ in range(7)]
    waiting = [t for t in await rows(app, RefreshToken) if t.used_at is None and not t.discarded_at]
    assert len(waiting) == 5
    for oldest in retries[:2]:
        assert (await token_row(app, oldest)).discarded_at is not None
    await ok(app, retries[-1])


# ---- reuse: a copied token signs the device out ----


async def test_old_token_after_moving_on_signs_the_device_out(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    b = await ok(app, a)
    c = await ok(app, b)  # the app has moved past a and b
    await rejected(app, a)  # a copy of a
    assert await device_revoked(app)
    await rejected(app, c)  # the real app is signed out too


async def test_discarded_token_signs_the_device_out(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    b1 = await ok(app, a)
    b2 = await ok(app, a)
    await ok(app, b1)  # b2 is discarded now
    await rejected(app, b2)
    assert await device_revoked(app)


# ---- other refusals ----


async def test_unknown_token(app: FastAPI) -> None:
    await logged_in(app)
    await rejected(app, "made-up")
    assert not await device_revoked(app)


async def test_expired_token(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    async with app.state.db.session() as s:
        await s.execute(update(RefreshToken).values(expires_at=datetime.now(UTC)))
        await s.commit()
    await rejected(app, a)
    assert not await device_revoked(app)


async def test_device_replaced_by_a_new_sms_login(app: FastAPI) -> None:
    first = await logged_in(app)
    await add_code(app, "654321")
    _, second = await verify(app, "654321")
    await rejected(app, first["refresh_token"])
    await ok(app, second["refresh_token"])


async def test_blocked_account(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    async with app.state.db.session() as s:
        await s.execute(update(User).values(status="blocked"))
        await s.commit()
    status, body = await refresh(app, a)
    assert (status, body["error"]) == (403, "account_blocked")


async def test_no_signing_key(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    app.state.keys = None
    status, body = await refresh(app, a)
    assert (status, body["error"]) == (503, "tokens_unavailable")


async def test_missing_token(app: FastAPI) -> None:
    async with http(app) as c:
        r = await c.post("/auth/token/refresh", json={})
    assert (r.status_code, r.json()["error"]) == (422, "invalid_request")


# ---- cleanup ----


async def test_cleanup_keeps_current_and_waiting_tokens(app: FastAPI) -> None:
    a = (await logged_in(app))["refresh_token"]
    b = await ok(app, a)
    c = await ok(app, b)  # now: a discarded, b current, c waiting
    long_ago = datetime.now(UTC) - timedelta(days=8)
    async with app.state.db.session() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.discarded_at.is_not(None))
            .values(discarded_at=long_ago)
        )
        await s.execute(
            update(RefreshToken).where(RefreshToken.used_at.is_not(None)).values(used_at=long_ago)
        )
        await s.commit()
    deleted = await delete_old_refresh_tokens(app.state.db, timedelta(days=7))
    assert deleted == 1  # only a
    await ok(app, b)  # the current token still works for a retry, after any time...
    await ok(app, c)  # ...and so does the waiting one


async def test_cleanup_deletes_expired_tokens(app: FastAPI) -> None:
    await logged_in(app)
    async with app.state.db.session() as s:
        await s.execute(update(RefreshToken).values(expires_at=datetime.now(UTC)))
        await s.commit()
    assert await delete_old_refresh_tokens(app.state.db, timedelta(days=7)) == 1


# ---- logs ----


async def test_logs_never_contain_tokens(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)
    a = (await logged_in(app))["refresh_token"]
    b = await ok(app, a)
    retry = await ok(app, a)
    await rejected(app, "made-up")
    output = "\n".join(JsonFormatter("auth-service", "test").format(r) for r in caplog.records)
    for event in ("tokens refreshed", "refresh retried", "refresh token rejected"):
        assert event in output
    for secret in (a, b, retry, CODE):
        assert secret not in output
