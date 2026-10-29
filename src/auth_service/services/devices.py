"""Devices, shared by login (services/login.py) and sign-up (services/registration.py).

WhatsApp model (PLAN.md section 6): a user has one **primary** device, a phone logged in by SMS.
A new SMS login replaces it: the old primary is revoked in the same transaction. Whoever gets hold
of a code (SIM swap, phishing) therefore signs the real owner out, so a takeover is noticed instead
of an extra device silently reading along. Linked devices (QR) come in step 10; listing and signing
out devices in step 6.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.db import repos

log = logging.getLogger(__name__)

SMS_PLATFORMS = frozenset({"ios", "android"})  # a primary device is a phone


@dataclass(frozen=True)
class DeviceInfo:
    platform: str  # "ios" | "android" | "web" (validated in api/)
    name: str | None


@dataclass(frozen=True)
class LoggedIn:
    """The result of a login or a sign-up: the device now exists and has an access token."""

    user_id: uuid.UUID
    device_id: uuid.UUID
    access_token: str
    expires_in: int


class UnsupportedPlatform(Exception):
    """SMS login is for phones only (a browser will be linked by QR, step 10)."""


class LoginConflict(Exception):
    """Another login for the same user won the race twice in a row; the app should retry."""


def check_sms_platform(device: DeviceInfo) -> None:
    """Call first, before anything is used up: only a phone can be a primary device."""
    if device.platform not in SMS_PLATFORMS:
        raise UnsupportedPlatform


async def add_primary(
    session: AsyncSession, user_id: uuid.UUID, device: DeviceInfo, now: datetime
) -> uuid.UUID:
    """Insert a primary device row; returns its id. Caller commits."""
    new = await repos.insert_device(
        session,
        user_id=user_id,
        platform=device.platform,
        name=device.name,
        role="primary",
        now=now,
    )
    return new.id


async def replace_primary(
    session: AsyncSession, user_id: uuid.UUID, device: DeviceInfo, now: datetime
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Revoke the user's active primary device and create the new one; returns (new id, old id).

    The unique index allows one active primary per user. If a parallel login for the same user
    commits its primary between our two statements, our INSERT fails: undo just these two steps
    (a savepoint, like a transaction inside the transaction) and try once more, which now revokes
    the winner. Twice in a row -> LoginConflict. Caller commits.
    """
    for _ in range(2):
        try:
            async with session.begin_nested():
                # Step 6: also revoke the old device's refresh tokens here.
                old_id = await repos.revoke_active_primary(session, user_id, now)
                new_id = await add_primary(session, user_id, device, now)
                return new_id, old_id
        except IntegrityError:
            log.warning("primary device race, retrying", extra={"user_id": str(user_id)})
    raise LoginConflict
