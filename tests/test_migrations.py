"""Migrations against a real Postgres: they apply, roll back, match the models, and the
constraints really protect the data. Skipped when TEST_DATABASE_URL is not set."""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from auth_service.db.engine import to_async_url
from auth_service.db.models import Device, OtpChallenge, SmsDailyUsage, User
from tests.conftest import TEST_DATABASE_URL, alembic, needs_db

pytestmark = needs_db


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    alembic("downgrade", "base")  # start from an empty database
    alembic("upgrade", "head")
    yield
    alembic("downgrade", "base")


@pytest.fixture
async def session(migrated: None) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(to_async_url(TEST_DATABASE_URL or ""))
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
        await s.rollback()
    async with engine.begin() as conn:  # leave the tables empty for the next test
        await conn.execute(text("TRUNCATE users, devices, otp_challenges, sms_daily_usage CASCADE"))
    await engine.dispose()


def test_upgrade_downgrade_roundtrip(migrated: None) -> None:
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    assert "(head)" in alembic("current")


def test_models_match_migrations(migrated: None) -> None:
    # Fails if someone changed db/models.py without generating a migration.
    alembic("check")


async def test_user_and_device(session: AsyncSession) -> None:
    user = User(phone_e164="+972501234567")
    session.add(user)
    await session.flush()
    session.add(Device(user_id=user.id, platform="ios", name="test phone"))
    await session.commit()
    await session.refresh(user)
    assert isinstance(user.id, uuid.UUID)
    assert user.id.version == 7  # time-ordered ids
    assert user.status == "active"  # server default


async def test_phone_is_unique(session: AsyncSession) -> None:
    session.add_all([User(phone_e164="+972501111111"), User(phone_e164="+972501111111")])
    with pytest.raises(IntegrityError, match="uq_users_phone_e164"):
        await session.commit()


@pytest.mark.parametrize("phone", ["0501234567", "+0501234567", "972501234567", "+97250abc"])
async def test_phone_must_be_e164(session: AsyncSession, phone: str) -> None:
    session.add(User(phone_e164=phone))
    with pytest.raises(IntegrityError, match="ck_users_phone_e164_format"):
        await session.commit()


async def test_device_platform_checked(session: AsyncSession) -> None:
    user = User(phone_e164="+972502222222")
    session.add(user)
    await session.flush()
    session.add(Device(user_id=user.id, platform="windows-phone"))
    with pytest.raises(IntegrityError, match="ck_devices_platform_valid"):
        await session.commit()


async def test_deleting_user_deletes_devices(session: AsyncSession) -> None:
    user = User(phone_e164="+972503333333")
    session.add(user)
    await session.flush()
    session.add(Device(user_id=user.id, platform="android"))
    await session.commit()
    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    await session.commit()
    remaining = await session.scalar(text("SELECT count(*) FROM devices"))
    assert remaining == 0


async def test_otp_challenge_defaults(session: AsyncSession) -> None:
    otp = OtpChallenge(
        phone_e164="+972504444444",
        code_hash=b"\x00" * 32,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        request_ip="10.0.0.1",
    )
    session.add(otp)
    await session.commit()
    await session.refresh(otp)
    assert otp.attempts == 0
    assert otp.consumed_at is None
    assert otp.created_at is not None


async def test_sms_daily_usage_never_negative(session: AsyncSession) -> None:
    session.add(SmsDailyUsage(day=date(2026, 10, 22), sent=-1))
    with pytest.raises(IntegrityError, match="ck_sms_daily_usage_sent_non_negative"):
        await session.commit()
