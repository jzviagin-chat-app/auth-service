"""Data access functions (one group per table): the only place with SQL.

They never commit: the caller (a service) decides where the transaction ends.
"""

from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.db.models import OtpChallenge, SmsDailyUsage

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


async def delete_otp_challenges_created_before(session: AsyncSession, cutoff: datetime) -> int:
    """Delete old challenges; returns how many rows went."""
    result = cast(
        CursorResult[Any],
        await session.execute(delete(OtpChallenge).where(OtpChallenge.created_at < cutoff)),
    )
    return result.rowcount
