"""Hourly housekeeping in every pod: delete old OTP challenges and long-revoked devices.

Every pod runs it; that's harmless, because deleting already-deleted rows does nothing.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from auth_service.config import Settings
from auth_service.db import repos
from auth_service.db.engine import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Retention:
    otp_challenges: timedelta  # also removes leftover registration tokens
    revoked_devices: timedelta
    interval_seconds: int

    @classmethod
    def from_settings(cls, s: Settings) -> Retention:
        return cls(
            otp_challenges=timedelta(hours=s.otp_retention_hours),
            revoked_devices=timedelta(days=s.device_retention_days),
            interval_seconds=s.otp_cleanup_interval_seconds,
        )


async def delete_old_challenges(db: Database, retention: timedelta) -> int:
    async with db.session() as session:
        cutoff = datetime.now(UTC) - retention
        deleted = await repos.delete_otp_challenges_created_before(session, cutoff)
        await session.commit()
    return deleted


async def delete_old_devices(db: Database, retention: timedelta) -> int:
    """Devices revoked (signed out or replaced by a new primary) more than `retention` ago."""
    async with db.session() as session:
        cutoff = datetime.now(UTC) - retention
        deleted = await repos.delete_devices_revoked_before(session, cutoff)
        await session.commit()
    return deleted


async def cleanup_loop(db: Database, retention: Retention) -> None:
    """Runs for the life of the process (started in main.lifespan), like a setInterval in Node."""
    while True:
        try:
            challenges = await delete_old_challenges(db, retention.otp_challenges)
            devices = await delete_old_devices(db, retention.revoked_devices)
            if challenges or devices:
                log.info("cleanup", extra={"otp_challenges": challenges, "devices": devices})
        except Exception:
            # Keep the loop alive (e.g. Postgres restarting); try again next round.
            log.exception("cleanup failed")
        await asyncio.sleep(retention.interval_seconds)
