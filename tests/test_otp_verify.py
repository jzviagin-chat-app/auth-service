"""POST /auth/otp/verify and POST /auth/register against a real Postgres.

Codes are inserted directly (we know them), except in the end-to-end test that goes through
POST /auth/otp/request too (that one also needs Redis).
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from auth_service.config import get_settings
from auth_service.core.logging import JsonFormatter
from auth_service.db import repos
from auth_service.db.models import Device, OtpChallenge, User
from auth_service.main import create_app
from auth_service.services import devices
from auth_service.services.cleanup import delete_old_devices
from auth_service.services.keys import SigningKeys
from auth_service.services.otp import hash_code
from auth_service.services.tokens import verify_access_token
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, alembic, needs_db, needs_redis

pytestmark = needs_db

PEPPER = "test-pepper"
PHONE = "+972501234567"
CODE = "123456"
DEVICE = {"platform": "ios", "name": "test phone"}
WEB = {"platform": "web", "name": "browser"}


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
            await conn.execute(text("TRUNCATE users, devices, otp_challenges, sms_daily_usage"))
        if app.state.redis is not None:
            await app.state.redis.flushdb()
        app.state.keys = SigningKeys(Ed25519PrivateKey.generate())
        yield app
    get_settings.cache_clear()


def http(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def add_code(
    app: FastAPI, code: str = CODE, phone: str = PHONE, expires_in: timedelta = timedelta(minutes=5)
) -> None:
    async with app.state.db.session() as s:
        s.add(
            OtpChallenge(
                phone_e164=phone,
                code_hash=hash_code(PEPPER.encode(), phone, code),
                expires_at=datetime.now(UTC) + expires_in,
            )
        )
        await s.commit()


async def add_user(app: FastAPI, phone: str = PHONE, status: str = "active") -> User:
    async with app.state.db.session() as s:
        user = User(phone_e164=phone, display_name="Existing", status=status)
        s.add(user)
        await s.commit()
        return user


async def verify(app: FastAPI, code: str = CODE, **extra: Any) -> tuple[int, dict[str, Any]]:
    body = {"phone": PHONE, "code": code, "device": DEVICE, **extra}
    async with http(app) as c:
        r = await c.post("/auth/otp/verify", json=body)
    return r.status_code, r.json()


async def register(
    app: FastAPI, token: str, name: str = "Jacob", device: dict[str, str] = DEVICE
) -> tuple[int, dict[str, Any]]:
    body = {"registration_token": token, "display_name": name, "device": device}
    async with http(app) as c:
        r = await c.post("/auth/register", json=body)
    return r.status_code, r.json()


async def rows[T](app: FastAPI, model: type[T]) -> list[T]:
    async with app.state.db.session() as s:
        return list((await s.scalars(select(model))).all())


async def the_challenge(app: FastAPI) -> OtpChallenge:
    [c] = await rows(app, OtpChallenge)
    return c


def check_token(app: FastAPI, body: dict[str, Any]) -> None:
    """The access token is valid and names the user and device from the response."""
    assert body["status"] == "logged_in"
    assert body["token_type"] == "Bearer"  # noqa: S105 (the scheme name)
    assert body["expires_in"] == 900
    claims = verify_access_token(app.state.keys, body["access_token"], issuer="auth-service")
    assert str(claims.user_id) == body["user_id"]
    assert str(claims.device_id) == body["device_id"]


# ---- new phone: verify -> register ----


async def test_new_phone_registers(app: FastAPI) -> None:
    await add_code(app)
    status, body = await verify(app)
    assert status == 200
    assert body["status"] == "registration_required"
    assert body["expires_in"] == 600
    assert await rows(app, User) == []  # no account until a display name is sent

    status, body = await register(app, body["registration_token"], name="  Jacob  ")
    assert status == 201
    check_token(app, body)
    [user] = await rows(app, User)
    [device] = await rows(app, Device)
    assert (user.phone_e164, user.display_name) == (PHONE, "Jacob")  # trimmed
    assert (device.user_id, device.platform, device.name) == (user.id, "ios", "test phone")
    assert str(device.id) == body["device_id"]


async def test_registration_token_works_once(app: FastAPI) -> None:
    await add_code(app)
    _, body = await verify(app)
    token = body["registration_token"]
    assert (await register(app, token))[0] == 201
    status, body = await register(app, token)
    assert (status, body["error"]) == (401, "invalid_registration_token")
    assert len(await rows(app, User)) == 1


async def test_registration_token_is_stored_hashed(app: FastAPI) -> None:
    await add_code(app)
    _, body = await verify(app)
    challenge = await the_challenge(app)
    assert challenge.registration_token_hash is not None
    assert body["registration_token"].encode() not in challenge.registration_token_hash


async def test_expired_registration_token(app: FastAPI) -> None:
    await add_code(app)
    _, body = await verify(app)
    async with app.state.db.session() as s:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await s.execute(update(OtpChallenge).values(registration_expires_at=past))
        await s.commit()
    status, body = await register(app, body["registration_token"])
    assert (status, body["error"]) == (401, "invalid_registration_token")


async def test_unknown_registration_token(app: FastAPI) -> None:
    status, body = await register(app, "made-up")
    assert (status, body["error"]) == (401, "invalid_registration_token")


async def test_phone_registered_in_the_meantime(app: FastAPI) -> None:
    # Two devices verify the same new number, then both try to register.
    await add_code(app, "111111")
    _, first = await verify(app, "111111")
    await add_code(app, "222222")
    _, second = await verify(app, "222222")
    assert (await register(app, first["registration_token"]))[0] == 201
    status, body = await register(app, second["registration_token"])
    assert (status, body["error"]) == (409, "already_registered")
    assert len(await rows(app, User)) == 1


@pytest.mark.parametrize("name", ["", "   ", "x" * 51])
async def test_display_name_is_validated(app: FastAPI, name: str) -> None:
    await add_code(app)
    _, body = await verify(app)
    status, body = await register(app, body["registration_token"], name=name)
    assert (status, body["error"]) == (422, "invalid_request")


# ---- known phone: logged in ----


async def test_known_phone_logs_in_with_a_new_device(app: FastAPI) -> None:
    user = await add_user(app)
    await add_code(app)
    status, body = await verify(app)
    assert status == 200
    check_token(app, body)
    assert body["user_id"] == str(user.id)
    [device] = await rows(app, Device)
    assert str(device.id) == body["device_id"]
    assert (device.role, device.revoked_at) == ("primary", None)
    assert device.last_seen_at is not None


async def active_primaries(app: FastAPI) -> list[Device]:
    return [d for d in await rows(app, Device) if d.role == "primary" and d.revoked_at is None]


async def test_new_sms_login_replaces_the_primary(app: FastAPI) -> None:
    # Register on one phone, then log in by SMS again (another phone, or the same one after a
    # reinstall: the same case). The first device is signed out.
    await add_code(app)
    _, reg = await verify(app)
    _, first = await register(app, reg["registration_token"])
    await add_code(app, "654321")
    status, second = await verify(app, "654321", device={"platform": "android"})
    assert status == 200
    assert second["user_id"] == first["user_id"]
    assert second["device_id"] != first["device_id"]

    by_id = {str(d.id): d for d in await rows(app, Device)}
    assert by_id[first["device_id"]].revoked_at is not None
    assert [str(d.id) for d in await active_primaries(app)] == [second["device_id"]]


async def test_web_cannot_log_in_by_sms(app: FastAPI) -> None:
    await add_code(app)
    status, body = await verify(app, device=WEB)
    assert (status, body["error"]) == (400, "unsupported_platform")
    challenge = await the_challenge(app)
    assert (challenge.consumed_at, challenge.attempts) == (None, 0)  # nothing used up
    assert (await verify(app))[0] == 200  # the code still works from a phone


async def test_web_cannot_register(app: FastAPI) -> None:
    await add_code(app)
    _, reg = await verify(app)
    status, body = await register(app, reg["registration_token"], device=WEB)
    assert (status, body["error"]) == (400, "unsupported_platform")
    assert (await register(app, reg["registration_token"]))[0] == 201  # token not used up


async def test_parallel_logins_leave_one_primary(app: FastAPI) -> None:
    # Two logins of the same user racing. (verify already serializes logins of one phone on its
    # newest code row; this is the database's own guarantee underneath.)
    user = await add_user(app)
    phone = devices.DeviceInfo("ios", None)

    async def one_login() -> None:
        async with app.state.db.session() as s:
            await devices.replace_primary(s, user.id, phone, datetime.now(UTC))
            await asyncio.sleep(0.05)  # hold the transaction open, so the two overlap
            await s.commit()

    # The second INSERT waits for the first transaction, fails on the index, and the retry
    # revokes the winner: both logins succeed, the later one is the primary.
    await asyncio.gather(one_login(), one_login())
    assert len(await active_primaries(app)) == 1
    assert len(await rows(app, Device)) == 2


async def test_race_lost_once_is_retried(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    user = await add_user(app)
    await add_code(app, "111111")
    await verify(app, "111111")  # an active primary exists
    real_revoke = repos.revoke_active_primary
    calls = 0

    async def revoke_too_late(*args: Any) -> Any:
        # The first try "misses" the existing primary, as if a parallel login had just
        # committed it: the INSERT hits the unique index.
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_revoke(*args)

    monkeypatch.setattr(repos, "revoke_active_primary", revoke_too_late)
    await add_code(app)
    status, body = await verify(app)
    assert status == 200 and calls == 2
    assert body["user_id"] == str(user.id)
    assert [str(d.id) for d in await active_primaries(app)] == [body["device_id"]]


async def test_race_lost_twice_answers_409(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_user(app)
    await add_code(app, "111111")
    await verify(app, "111111")

    async def never_revoke(*args: Any) -> None:
        return None

    monkeypatch.setattr(repos, "revoke_active_primary", never_revoke)
    await add_code(app)
    status, body = await verify(app)
    assert (status, body["error"]) == (409, "login_conflict")
    challenges = sorted(await rows(app, OtpChallenge), key=lambda c: c.created_at)
    assert challenges[-1].consumed_at is None  # rolled back: the app can retry with the same code


async def test_cleanup_deletes_long_revoked_devices(app: FastAPI) -> None:
    user = await add_user(app)
    now = datetime.now(UTC)
    async with app.state.db.session() as s:
        for name, revoked in (
            ("old", now - timedelta(days=31)),
            ("recent", now - timedelta(days=1)),
            ("active", None),
        ):
            s.add(Device(user_id=user.id, platform="ios", name=name, revoked_at=revoked))
        await s.commit()
    assert await delete_old_devices(app.state.db, timedelta(days=30)) == 1
    assert sorted(d.name or "" for d in await rows(app, Device)) == ["active", "recent"]


async def test_blocked_account(app: FastAPI) -> None:
    await add_user(app, status="blocked")
    await add_code(app)
    status, body = await verify(app)
    assert (status, body["error"]) == (403, "account_blocked")
    assert (await the_challenge(app)).consumed_at is not None  # the code is used up anyway
    assert await rows(app, Device) == []


# ---- wrong, expired, used codes ----


async def test_wrong_code_counts_attempts(app: FastAPI) -> None:
    await add_code(app)
    status, body = await verify(app, "000000")
    assert (status, body["error"]) == (400, "invalid_code")
    assert "4 attempts left" in body["message"]
    assert (await the_challenge(app)).attempts == 1
    assert (await verify(app))[0] == 200  # the right code still works


async def test_five_wrong_codes_burn_the_code(app: FastAPI) -> None:
    await add_code(app)
    for _ in range(4):
        assert (await verify(app, "000000"))[1]["error"] == "invalid_code"
    assert (await verify(app, "000000"))[1]["error"] == "code_expired"  # 5th: nothing left
    status, body = await verify(app)  # even the right code
    assert (status, body["error"]) == (400, "code_expired")


async def test_expired_code(app: FastAPI) -> None:
    await add_code(app, expires_in=timedelta(seconds=-1))
    status, body = await verify(app)
    assert (status, body["error"]) == (400, "code_expired")


async def test_no_code_requested(app: FastAPI) -> None:
    status, body = await verify(app)
    assert (status, body["error"]) == (400, "code_expired")


async def test_code_works_once(app: FastAPI) -> None:
    await add_code(app)
    assert (await verify(app))[0] == 200
    status, body = await verify(app)
    assert (status, body["error"]) == (400, "code_expired")


async def test_only_the_newest_code_counts(app: FastAPI) -> None:
    await add_code(app, "111111")
    await add_code(app, "222222")  # a resend
    assert (await verify(app, "111111"))[1]["error"] == "invalid_code"
    assert (await verify(app, "222222"))[0] == 200


async def test_code_of_another_phone_does_not_work(app: FastAPI) -> None:
    await add_code(app, phone="+972509999999")
    status, body = await verify(app)
    assert (status, body["error"]) == (400, "code_expired")


async def test_parallel_verifies_use_the_code_once(app: FastAPI) -> None:
    await add_user(app)
    await add_code(app)
    results = await asyncio.gather(*(verify(app) for _ in range(5)))
    assert sorted(status for status, _ in results) == [200, 400, 400, 400, 400]
    assert len(await rows(app, Device)) == 1


# ---- input checks and outages ----


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "١٢٣٤٥٦"])
async def test_code_format(app: FastAPI, code: str) -> None:
    status, body = await verify(app, code)
    assert (status, body["error"]) == (422, "invalid_request")


async def test_invalid_phone(app: FastAPI) -> None:
    status, body = await verify(app, phone="+15551234567")
    assert (status, body["error"]) == (400, "invalid_phone")


async def test_invalid_platform(app: FastAPI) -> None:
    status, body = await verify(app, device={"platform": "windows"})
    assert (status, body["error"]) == (422, "invalid_request")


async def test_no_signing_key_answers_503_and_keeps_the_code(app: FastAPI) -> None:
    app.state.keys = None
    await add_code(app)
    status, body = await verify(app)
    assert (status, body["error"]) == (503, "tokens_unavailable")
    challenge = await the_challenge(app)
    assert (challenge.consumed_at, challenge.attempts) == (None, 0)
    status, body = await register(app, "anything")
    assert (status, body["error"]) == (503, "tokens_unavailable")


async def test_logs_never_contain_secrets(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)

    await add_code(app)
    await verify(app, "000000")  # wrong
    _, reg = await verify(app)  # registration required
    _, logged = await register(app, reg["registration_token"], name="Secret Name")
    await add_code(app, "654321")
    _, again = await verify(app, "654321")  # logged in

    output = "\n".join(JsonFormatter("auth-service", "test").format(r) for r in caplog.records)
    for event in ("otp wrong code", "registration required", "user registered", "logged in"):
        assert event in output
    assert "+97250***4567" in output
    for secret in (
        "972501234567",
        CODE,
        "654321",
        reg["registration_token"],
        logged["access_token"],
        again["access_token"],
        "Secret Name",
    ):
        assert secret not in output


# ---- the whole flow, from the SMS ----


@needs_redis
async def test_end_to_end_from_otp_request(app: FastAPI) -> None:
    sent: list[str] = []

    class Sms:
        name = "recording"

        async def send(self, phone_e164: str, message: str) -> str:
            sent.append(message)
            return "msg-1"

        async def aclose(self) -> None:
            return None

    app.state.sms = Sms()
    async with http(app) as c:
        assert (await c.post("/auth/otp/request", json={"phone": PHONE})).status_code == 202
    match = re.search(r"[0-9]{6}", sent[-1])
    assert match

    _, body = await verify(app, match.group())
    status, body = await register(app, body["registration_token"])
    assert status == 201
    check_token(app, body)
