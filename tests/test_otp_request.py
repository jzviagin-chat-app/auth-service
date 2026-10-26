"""POST /auth/otp/request end to end: real Postgres + real Redis, a recording SMS sender.

Skipped unless both TEST_DATABASE_URL and TEST_REDIS_URL are set (README, "Everyday commands").
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, text

from auth_service.config import get_settings
from auth_service.core.logging import JsonFormatter
from auth_service.db.models import OtpChallenge, SmsDailyUsage
from auth_service.main import create_app
from auth_service.services.otp import delete_old_challenges, hash_code
from auth_service.services.sms import GlobalSmsSender, SmsSendError
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, alembic, needs_db, needs_redis

pytestmark = [needs_db, needs_redis]

PEPPER = "test-pepper"
PHONE = "+972501234567"
CLIENT_IP = "203.0.113.7"


class RecordingSms:
    """Stands in for the SMS provider: remembers what it 'sent', or fails on demand."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send(self, phone_e164: str, text: str) -> str:
        if self.fail:
            raise SmsSendError("http_500")
        self.sent.append((phone_e164, text))
        return f"msg-{len(self.sent)}"

    async def aclose(self) -> None:
        return None

    def last_code(self) -> str:
        match = re.search(r"[0-9]{6}", self.sent[-1][1])
        assert match
        return match.group()


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    alembic("upgrade", "head")
    yield


@pytest.fixture
async def app(migrated: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """The real app with its lifespan running (DB pool, Redis client, cleanup task), on the test
    database and test Redis, both wiped before each test."""
    for name, value in {
        "ENVIRONMENT": "prod",
        "DATABASE_URL": TEST_DATABASE_URL or "",
        "REDIS_URL": TEST_REDIS_URL or "",
        "OTP_PEPPER": PEPPER,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        await app.state.redis.flushdb()
        async with app.state.db.engine.begin() as conn:
            await conn.execute(text("TRUNCATE otp_challenges, sms_daily_usage"))
        app.state.sms = RecordingSms()
        yield app
    get_settings.cache_clear()


def http(app: FastAPI, ip: str = CLIENT_IP) -> AsyncClient:
    transport = ASGITransport(app=app, client=(ip, 1234))
    return AsyncClient(transport=transport, base_url="http://test")


async def request_code(
    app: FastAPI, phone: str = PHONE, ip: str = CLIENT_IP
) -> tuple[int, dict[str, Any]]:
    """POST /auth/otp/request; returns (status, {"json": body, "retry_after": header or None})."""
    async with http(app, ip) as c:
        r = await c.post("/auth/otp/request", json={"phone": phone})
    return r.status_code, {"json": r.json(), "retry_after": r.headers.get("Retry-After")}


def set_setting(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Change a setting for this test (limits are read on every request)."""
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def end_cooldown(app: FastAPI) -> None:
    """Skip the 60 s resend cooldown, to reach the hourly/daily limits quickly."""
    redis: Redis = app.state.redis
    keys = await redis.keys("auth:rl:phone:*:cooldown")
    if keys:
        await redis.delete(*keys)


async def challenges(app: FastAPI) -> list[OtpChallenge]:
    async with app.state.db.session() as s:
        return list((await s.scalars(select(OtpChallenge))).all())


async def sms_sent_today(app: FastAPI) -> int:
    async with app.state.db.session() as s:
        row = await s.get(SmsDailyUsage, datetime.now(UTC).date())
        return row.sent if row else 0


# ---- the happy path ----


async def test_sends_code_and_stores_only_its_hash(app: FastAPI) -> None:
    status, r = await request_code(app)
    assert status == 202
    assert r["json"] == {"resend_after_seconds": 60, "expires_in_seconds": 300}

    sms: RecordingSms = app.state.sms
    assert [phone for phone, _ in sms.sent] == [PHONE]
    code = sms.last_code()

    [row] = await challenges(app)
    assert row.phone_e164 == PHONE
    assert row.code_hash == hash_code(PEPPER.encode(), PHONE, code)  # the hash, never the code
    assert code.encode() not in row.code_hash
    assert str(row.request_ip) == CLIENT_IP
    assert timedelta(minutes=4) < row.expires_at - row.created_at <= timedelta(minutes=5, seconds=5)
    assert await sms_sent_today(app) == 1


async def test_redis_keys_never_contain_the_phone(app: FastAPI) -> None:
    await request_code(app)
    keys = [k.decode() for k in await app.state.redis.keys("*")]
    assert keys  # the counters exist...
    assert all(k.startswith("auth:rl:") for k in keys)
    assert not any("972501234567" in k for k in keys)  # ...but only with the phone's HMAC


# ---- bad input ----


@pytest.mark.parametrize("phone", ["0501234567", "+14155550100", "+97221234567"])
async def test_invalid_phone(app: FastAPI, phone: str) -> None:
    status, r = await request_code(app, phone)
    assert status == 400
    assert r["json"]["error"] == "invalid_phone"
    assert app.state.sms.sent == []
    assert await challenges(app) == []


# ---- limits ----


async def test_resend_cooldown(app: FastAPI) -> None:
    assert (await request_code(app))[0] == 202
    status, r = await request_code(app)
    assert status == 429
    assert r["json"]["error"] == "rate_limited"
    assert 1 <= int(r["retry_after"]) <= 60
    assert len(app.state.sms.sent) == 1


async def test_phone_hourly_limit(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    set_setting(monkeypatch, "OTP_PHONE_MAX_PER_HOUR", "2")
    for _ in range(2):
        assert (await request_code(app))[0] == 202
        await end_cooldown(app)
    status, r = await request_code(app)
    assert status == 429
    assert 60 < int(r["retry_after"]) <= 3600  # wait for the hour window, not the cooldown
    assert len(app.state.sms.sent) == 2


async def test_ip_hourly_limit(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    set_setting(monkeypatch, "OTP_IP_MAX_PER_HOUR", "2")
    assert (await request_code(app, "+972501111111"))[0] == 202
    assert (await request_code(app, "+972502222222"))[0] == 202
    assert (await request_code(app, "+972503333333"))[0] == 429  # same IP, third phone
    await end_cooldown(app)  # the rejected request still started this phone's cooldown
    assert (await request_code(app, "+972503333333", ip="198.51.100.1"))[0] == 202  # other IP


async def test_daily_cap(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    set_setting(monkeypatch, "FAKE_SMS_DAILY_CAP", "1")
    assert (await request_code(app, "+972501111111"))[0] == 202
    status, r = await request_code(app, "+972502222222")
    assert status == 503
    assert r["json"]["error"] == "sms_unavailable"
    assert await sms_sent_today(app) == 1  # never over the cap
    assert len(await challenges(app)) == 1
    assert len(app.state.sms.sent) == 1


# ---- failures ----


async def test_redis_down_fails_closed(app: FastAPI) -> None:
    working = app.state.redis
    app.state.redis = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    try:
        status, r = await request_code(app)
    finally:
        await app.state.redis.aclose()
        app.state.redis = working
    assert status == 503
    assert r["json"]["error"] == "sms_unavailable"
    assert app.state.sms.sent == []
    assert await challenges(app) == []


async def test_provider_failure(app: FastAPI) -> None:
    app.state.sms.fail = True
    status, r = await request_code(app)
    assert status == 503
    assert r["json"]["error"] == "sms_unavailable"


# ---- logs ----


async def test_logs_never_contain_the_phone_or_code(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)

    await request_code(app)  # sent
    code = app.state.sms.last_code()
    await request_code(app)  # 429 cooldown
    await end_cooldown(app)
    app.state.sms.fail = True
    await request_code(app)  # provider failure
    app.state.sms.fail = False
    set_setting(monkeypatch, "FAKE_SMS_DAILY_CAP", "1")
    await end_cooldown(app)
    await request_code(app)  # 503 daily cap

    formatter = JsonFormatter("auth-service", "test")
    lines = [formatter.format(r) for r in caplog.records]
    output = "\n".join(lines)
    for event in ("otp requested", "sms sent", "otp rate limited", "sms send failed"):
        assert event in output
    assert "sms daily cap reached" in output
    assert "+97250***4567" in output  # masked form is there...
    assert "972501234567" not in output  # ...the full number never
    assert code not in output


def test_database_errors_hide_query_values(app: FastAPI) -> None:
    assert app.state.db.engine.sync_engine.hide_parameters


# ---- cleanup job ----


async def test_cleanup_deletes_only_old_challenges(app: FastAPI) -> None:
    now = datetime.now(UTC)
    async with app.state.db.session() as s:
        for phone, age in (("+972501111111", timedelta(hours=25)), ("+972502222222", timedelta(0))):
            s.add(
                OtpChallenge(
                    phone_e164=phone,
                    code_hash=b"\x00" * 32,
                    created_at=now - age,
                    expires_at=now - age + timedelta(minutes=5),
                )
            )
        await s.commit()

    await delete_old_challenges(app.state.db, timedelta(hours=24))
    assert [c.phone_e164 for c in await challenges(app)] == ["+972502222222"]


# ---- the real Global SMS sender, provider mocked at the HTTP level ----


def globalsms_answering(status: int, text: str, seen: list[httpx.Request]) -> GlobalSmsSender:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=text)

    return GlobalSmsSender(
        api_key="secret-api-key",
        originator="MyApp",
        base_url="https://sms.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


async def test_globalsms_end_to_end(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    real_answer = '{"success":true,"result":1,"resultJSON":"[]","errDesc":""}'  # seen live
    app.state.sms = globalsms_answering(200, real_answer, seen)  # closed by the app's shutdown

    status, _ = await request_code(app)
    assert status == 202

    [request] = seen
    body = json.loads(request.content)
    assert body["destinations"] == "0501234567"
    code = re.search(r"[0-9]{6}", body["txtSMSmessage"])
    assert code
    [row] = await challenges(app)
    assert row.code_hash == hash_code(PEPPER.encode(), PHONE, code.group())

    output = "\n".join(JsonFormatter("auth-service", "test").format(r) for r in caplog.records)
    assert '"provider": "globalsms"' in output
    assert "secret-api-key" not in output
    assert "501234567" not in output  # neither +972501234567 nor 0501234567
    assert code.group() not in output


async def test_globalsms_refusal_is_503(app: FastAPI) -> None:
    app.state.sms = globalsms_answering(403, "", [])  # e.g. our IP isn't approved
    status, r = await request_code(app)
    assert status == 503
    assert r["json"]["error"] == "sms_unavailable"
