"""Devices, shared by login (services/login.py) and sign-up (services/registration.py).

WhatsApp model (PLAN.md section 6): a user has one **primary** device, a phone logged in by SMS.
A new SMS login replaces it: the old primary is revoked in the same transaction. Whoever gets hold
of a code (SIM swap, phishing) therefore signs the real owner out, so a takeover is noticed instead
of an extra device silently reading along. Linked devices (QR) come in step 10.

Signing a device out (`revoked_at`) happens on logout, when the primary removes a linked device,
when a new SMS login replaces the primary, and on refresh-token reuse, always through
services/revocation.revoke_device (which also publishes it for chat-service after the commit). It
ends the device's refresh tokens (services/refresh.py checks the device first), and auth's endpoints
refuse its access token (api/deps.CurrentDevice).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.db import repos
from auth_service.services.revocation import RevokedDevices, revoke_device

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
    refresh_token: str


class AccountBlocked(Exception):
    """The user's status is "blocked": no login, no refresh."""


class UnsupportedPlatform(Exception):
    """SMS login is for phones only (a browser will be linked by QR, step 10)."""


class LoginConflict(Exception):
    """Another login for the same user won the race twice in a row; the app should retry."""


class ActionNotAllowed(Exception):
    """Only the primary manages devices, and it can't remove itself (it logs out instead)."""


class DeviceNotFound(Exception):
    """No such device of this user (another user's device looks the same: nothing is revealed)."""


@dataclass(frozen=True)
class DeviceView:
    id: uuid.UUID
    platform: str
    name: str | None
    role: str
    created_at: datetime
    last_seen_at: datetime | None
    current: bool  # the calling device


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
    session: AsyncSession,
    user_id: uuid.UUID,
    device: DeviceInfo,
    revoked: RevokedDevices,
    now: datetime,
) -> uuid.UUID:
    """Revoke the user's active primary device and create the new one; returns the new id.

    The unique index allows one active primary per user. If a parallel login for the same user
    commits its primary before our INSERT, the INSERT fails: undo just the INSERT (a savepoint, like
    a transaction inside the transaction), revoke the winner and try once more. Twice in a row ->
    LoginConflict. Revocations stay outside the savepoint, so none of them is ever undone on its
    own (services/revocation.py relies on that). Caller commits.
    """
    for _ in range(2):
        old_id = await repos.active_primary_id(session, user_id)
        if old_id is not None:
            await revoke_device(session, revoked, old_id, now, "replaced_by_new_login")
        try:
            async with session.begin_nested():
                return await add_primary(session, user_id, device, now)
        except IntegrityError:
            log.warning("primary device race, retrying", extra={"user_id": str(user_id)})
    raise LoginConflict


async def log_out(
    session: AsyncSession,
    device_id: uuid.UUID,
    revoked: RevokedDevices,
    now: datetime,
    reason: str = "logout",
) -> None:
    """Sign the device out. Already signed out: nothing to do (logout always succeeds)."""
    if await revoke_device(session, revoked, device_id, now, reason):
        await session.commit()
    else:
        await session.rollback()


async def list_devices(
    session: AsyncSession, *, user_id: uuid.UUID, caller_device_id: uuid.UUID, caller_role: str
) -> list[DeviceView]:
    """The user's active devices; only the primary may ask (WhatsApp model)."""
    if caller_role != "primary":
        raise ActionNotAllowed
    return [
        DeviceView(
            id=d.id,
            platform=d.platform,
            name=d.name,
            role=d.role,
            created_at=d.created_at,
            last_seen_at=d.last_seen_at,
            current=d.id == caller_device_id,
        )
        for d in await repos.active_devices(session, user_id)
    ]


async def remove_device(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    caller_device_id: uuid.UUID,
    caller_role: str,
    device_id: uuid.UUID,
    revoked: RevokedDevices,
    now: datetime,
) -> None:
    """The primary signs out one of the user's linked devices. Already signed out: success."""
    if caller_role != "primary" or device_id == caller_device_id:
        raise ActionNotAllowed
    target = await repos.get_device(session, device_id)
    if target is None or target.user_id != user_id:
        raise DeviceNotFound
    if target.revoked_at is not None:
        return
    if target.role != "linked":
        raise ActionNotAllowed  # can't happen: the caller is the only active primary
    await log_out(session, device_id, revoked, now, reason="removed_by_primary")
