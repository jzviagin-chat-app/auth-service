"""Refresh tokens (README.md, "Sessions"): POST /auth/token/refresh.

The app gets a refresh token at login and swaps it, about every 15 minutes, for a new one plus a new
access token. Every login creates a new device, so a device's tokens form one chain. Each token is
  waiting   (never used: the newest one handed out),
  current   (used; the app may retry with it if the reply got lost),
  discarded (replaced: using it again means someone holds a copy).

For a token T (the device row is locked, so refreshes of one device run one at a time):
  unknown, expired, device signed out -> InvalidRefreshToken
  T discarded                         -> theft: the device is signed out
  T waiting                           -> T becomes current; every other token of the device is
                                         discarded (the old current one, other waiting ones)
  T current                           -> the app never got the reply: one more waiting token

So a lost reply never logs anyone out (retry with the same token, after any delay), and replies
arriving out of order are fine (whichever new token the app uses first wins). A copied token gives
itself away as soon as the app and the copy have both been used.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.config import Settings
from auth_service.core.hashing import hash_secret, new_secret_token
from auth_service.db import repos
from auth_service.services.devices import AccountBlocked, LoggedIn
from auth_service.services.tokens import TokenIssuer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshPolicy:
    ttl: timedelta  # each new token lives this long, so the session slides with use
    max_pending: int  # waiting tokens kept at once (retries); the oldest is discarded beyond

    @classmethod
    def from_settings(cls, s: Settings) -> RefreshPolicy:
        return cls(ttl=timedelta(days=s.refresh_token_ttl_days), max_pending=s.refresh_max_pending)


class InvalidRefreshToken(Exception):
    """The app must log in again. `reason` is for the logs only (the app gets one error code)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def issue_refresh_token(
    session: AsyncSession, device_id: uuid.UUID, policy: RefreshPolicy, now: datetime
) -> str:
    """A new waiting token for the device; only its hash is stored. Caller commits."""
    token = new_secret_token()
    await repos.insert_refresh_token(
        session, device_id=device_id, token_hash=hash_secret(token), expires_at=now + policy.ttl
    )
    return token


async def refresh(
    *,
    refresh_token: str,
    session: AsyncSession,
    tokens: TokenIssuer,
    policy: RefreshPolicy,
    now: datetime,
) -> LoggedIn:
    token_hash = hash_secret(refresh_token)
    found = await repos.refresh_token_by_hash(session, token_hash)
    if found is None:
        await _reject(session, "unknown")
    device_id = found.device_id

    # Lock the device, then read the token again: a refresh that held the lock before us may
    # have changed it.
    locked = await repos.lock_device(session, device_id)
    found = await repos.refresh_token_by_hash(session, token_hash)
    if locked is None or found is None:
        await _reject(session, "unknown")
    device, user_status = locked
    log_ids = {"device_id": str(device_id), "user_id": str(device.user_id)}

    if device.revoked_at is not None:
        await _reject(session, "device_revoked", log_ids)
    if found.expires_at <= now:
        await _reject(session, "expired", log_ids)
    if found.discarded_at is not None:
        # Someone used a replaced token: either a copy, or the real app holds the copy and the
        # thief went first. We can't tell which, so the device is signed out (both log in again).
        device.revoked_at = now
        await session.commit()
        log.warning("refresh token reused: device signed out", extra=log_ids)
        raise InvalidRefreshToken("reused")
    if user_status != "active":
        await session.rollback()
        log.warning("refresh refused: account blocked", extra=log_ids)
        raise AccountBlocked

    live = await repos.live_refresh_tokens(session, device_id)
    if found.used_at is None:
        # A waiting token: the app got it, so everything else of this device is dead now.
        found.used_at = now
        for other in live:
            if other.id != found.id:
                other.discarded_at = now
    else:
        # The current token again: the app never got (or never saved) the last reply. Hand out
        # one more waiting token, keeping the others usable (replies may arrive out of order).
        waiting = [t for t in live if t.used_at is None]
        for old in waiting[: max(0, len(waiting) - policy.max_pending + 1)]:
            old.discarded_at = now
        log.info("refresh retried with the current token", extra=log_ids)

    new_token = await issue_refresh_token(session, device_id, policy, now)
    device.last_seen_at = now
    user_id = device.user_id
    await session.commit()

    access_token, expires_in = tokens.issue(user_id=user_id, device_id=device_id)
    log.info("tokens refreshed", extra=log_ids)
    return LoggedIn(user_id, device_id, access_token, expires_in, new_token)


async def _reject(
    session: AsyncSession, reason: str, log_ids: dict[str, str] | None = None
) -> NoReturn:
    await session.rollback()
    log.info("refresh token rejected", extra={"reason": reason, **(log_ids or {})})
    raise InvalidRefreshToken(reason)
