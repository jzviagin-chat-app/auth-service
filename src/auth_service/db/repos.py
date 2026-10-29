"""Data access functions (one group per table): the only place with SQL.

They never commit: the caller (a service) decides where the transaction ends.
"""

import uuid
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.db.models import Device, OtpChallenge, SmsDailyUsage, User

# ---- sms_daily_usage ----


async def reserve_daily_sms(session: AsyncSession, day: date, cap: int) -> bool:
    """Count one SMS for `day` if we're still under `cap`. False = cap reached, don't send.

    Exact even when several pods race: the UPDATE locks the day's row, and Postgres re-checks
    `sent < cap` after waiting for the lock, so the count can never pass the cap. The lock is held
    until the caller commits.
    """
    # Make sure the day's row exists (a no-op if another request already created it).
    await session.execute(
        insert(SmsDailyUsage).values(day=day, sent=0).on_conflict_do_nothing(index_elements=["day"])
    )
    result = await session.execute(
        update(SmsDailyUsage)
        .where(SmsDailyUsage.day == day, SmsDailyUsage.sent < cap)
        .values(sent=SmsDailyUsage.sent + 1)
        .returning(SmsDailyUsage.sent)
    )
    return result.scalar_one_or_none() is not None


# ---- otp_challenges ----


async def insert_otp_challenge(
    session: AsyncSession,
    *,
    phone_e164: str,
    code_hash: bytes,
    expires_at: datetime,
    request_ip: str | None,
) -> OtpChallenge:
    challenge = OtpChallenge(
        phone_e164=phone_e164, code_hash=code_hash, expires_at=expires_at, request_ip=request_ip
    )
    session.add(challenge)
    await session.flush()  # sends the INSERT now (inside the transaction), so errors surface here
    return challenge


async def latest_otp_challenge_for_update(
    session: AsyncSession, phone_e164: str
) -> OtpChallenge | None:
    """The newest challenge for this phone, locked until the caller commits (SELECT ... FOR UPDATE).

    The lock makes two parallel verify requests for one phone take turns, so a code can't be used
    twice and every wrong attempt is counted.
    """
    result = await session.scalars(
        select(OtpChallenge)
        .where(OtpChallenge.phone_e164 == phone_e164)
        .order_by(OtpChallenge.created_at.desc(), OtpChallenge.id.desc())
        .limit(1)
        .with_for_update()
    )
    return result.first()


async def challenge_by_registration_hash_for_update(
    session: AsyncSession, token_hash: bytes
) -> OtpChallenge | None:
    """The challenge holding this registration token, locked until the caller commits."""
    result = await session.scalars(
        select(OtpChallenge)
        .where(OtpChallenge.registration_token_hash == token_hash)
        .with_for_update()
    )
    return result.first()


async def delete_otp_challenges_created_before(session: AsyncSession, cutoff: datetime) -> int:
    """Delete old challenges; returns how many rows went."""
    result = cast(
        CursorResult[Any],
        await session.execute(delete(OtpChallenge).where(OtpChallenge.created_at < cutoff)),
    )
    return result.rowcount


# ---- users / devices ----


async def get_user_by_phone(session: AsyncSession, phone_e164: str) -> User | None:
    result = await session.scalars(select(User).where(User.phone_e164 == phone_e164))
    return result.first()


async def insert_user(session: AsyncSession, *, phone_e164: str, display_name: str) -> User:
    """Raises IntegrityError (at the flush) if the phone already has an account."""
    user = User(phone_e164=phone_e164, display_name=display_name)
    session.add(user)
    await session.flush()
    return user


async def insert_device(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    platform: str,
    name: str | None,
    role: str,
    now: datetime,
) -> Device:
    """Raises IntegrityError (at the flush) for a second active primary of the same user."""
    device = Device(user_id=user_id, platform=platform, name=name, role=role, last_seen_at=now)
    session.add(device)
    await session.flush()
    return device


async def revoke_active_primary(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> uuid.UUID | None:
    """Sign out the user's current primary device; returns its id (None if there was none)."""
    result = await session.execute(
        update(Device)
        .where(Device.user_id == user_id, Device.role == "primary", Device.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(Device.id)
    )
    return result.scalar_one_or_none()


async def delete_devices_revoked_before(session: AsyncSession, cutoff: datetime) -> int:
    result = cast(
        CursorResult[Any],
        await session.execute(delete(Device).where(Device.revoked_at < cutoff)),
    )
    return result.rowcount
