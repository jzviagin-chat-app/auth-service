"""Checking an SMS code (POST /auth/otp/verify), then logging in or starting a sign-up.

    otp/verify {phone, code, device}
      wrong code             -> InvalidCode (attempt counted)
      right code, known user -> LoggedIn (this phone replaces the primary device + access and
                                refresh tokens)
      right code, new phone  -> RegistrationRequired (services/registration.py takes over)

Devices (the primary-device rule) are in services/devices.py. No HTTP here (api/ maps the
exceptions) and no SQL (db/repos.py).
"""

import hmac
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.core.masking import mask_phone
from auth_service.db import repos
from auth_service.services.devices import (
    AccountBlocked,
    DeviceInfo,
    LoggedIn,
    check_sms_platform,
    replace_primary,
)
from auth_service.services.otp import InvalidPhone, hash_code, is_israeli_mobile
from auth_service.services.refresh import RefreshPolicy, issue_refresh_token
from auth_service.services.registration import RegistrationRequired, start_registration
from auth_service.services.tokens import TokenIssuer

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # wrong tries per code; then a new code must be requested


class InvalidCode(Exception):
    """Wrong code; the same code may be retried `attempts_left` more times."""

    def __init__(self, attempts_left: int) -> None:
        super().__init__(attempts_left)
        self.attempts_left = attempts_left


class CodeExpired(Exception):
    """No usable code: none sent, expired, already used, or too many wrong tries."""


async def verify_code(
    *,
    phone: str,
    code: str,
    device: DeviceInfo,
    session: AsyncSession,
    pepper: bytes,
    tokens: TokenIssuer,
    refresh: RefreshPolicy,
    now: datetime,
) -> LoggedIn | RegistrationRequired:
    if not is_israeli_mobile(phone):
        raise InvalidPhone
    check_sms_platform(device)  # before the code is touched: nothing is used up
    masked = mask_phone(phone)

    # Only the newest code counts: requesting a new one cancels the previous code.
    challenge = await repos.latest_otp_challenge_for_update(session, phone)
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.expires_at <= now
        or challenge.attempts >= MAX_ATTEMPTS
    ):
        await session.rollback()
        log.info("otp code expired", extra={"phone": masked})
        raise CodeExpired

    # compare_digest takes the same time wherever the bytes differ, so response timing can't
    # leak how much of the hash matched.
    if not hmac.compare_digest(hash_code(pepper, phone, code), challenge.code_hash):
        challenge.attempts += 1
        await session.commit()
        attempts_left = MAX_ATTEMPTS - challenge.attempts
        log.warning("otp wrong code", extra={"phone": masked, "attempts_left": attempts_left})
        if attempts_left == 0:
            raise CodeExpired  # nothing left to retry: tell the app to request a new code now
        raise InvalidCode(attempts_left)

    challenge.consumed_at = now  # single use
    user = await repos.get_user_by_phone(session, phone)

    if user is None:
        result = start_registration(challenge, now)
        await session.commit()
        log.info("registration required", extra={"phone": masked})
        return result

    if user.status != "active":
        await session.commit()  # the code is used up all the same
        log.warning("login refused: account blocked", extra={"user_id": str(user.id)})
        raise AccountBlocked

    user_id = user.id
    new_device_id, old_device_id = await replace_primary(session, user_id, device, now)
    refresh_token = await issue_refresh_token(session, new_device_id, refresh, now)
    await session.commit()
    access_token, expires_in = tokens.issue(user_id=user_id, device_id=new_device_id)
    log.info(
        "logged in",
        extra={
            "user_id": str(user_id),
            "device_id": str(new_device_id),
            "replaced_device_id": str(old_device_id) if old_device_id else None,
            "phone": masked,
        },
    )
    return LoggedIn(user_id, new_device_id, access_token, expires_in, refresh_token)
