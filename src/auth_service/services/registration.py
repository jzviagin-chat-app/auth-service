"""Sign-up for a new phone number (Telegram-like; README.md, "API").

    otp/verify, right code, new phone -> start_registration(): a short-lived registration token
    register {registration_token, display_name, device}
                                      -> LoggedIn (new user + primary device + tokens)

No user row exists until `register`, so a code typed by mistake on someone else's phone never
creates an account. No HTTP here (api/ maps the exceptions) and no SQL (db/repos.py).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.core.hashing import hash_secret, new_secret_token
from auth_service.core.masking import mask_phone
from auth_service.db import repos
from auth_service.db.models import OtpChallenge
from auth_service.services.devices import DeviceInfo, LoggedIn, add_primary, check_sms_platform
from auth_service.services.refresh import RefreshPolicy, issue_refresh_token
from auth_service.services.tokens import TokenIssuer

log = logging.getLogger(__name__)

REGISTRATION_TTL = timedelta(minutes=10)  # time to type a display name after the code


@dataclass(frozen=True)
class RegistrationRequired:
    registration_token: str
    expires_in: int


class InvalidRegistrationToken(Exception):
    """Unknown, expired or already used."""


class AlreadyRegistered(Exception):
    """The phone got an account in the meantime (e.g. a second device registered first)."""


def start_registration(challenge: OtpChallenge, now: datetime) -> RegistrationRequired:
    """Called by verify_code for a right code and a phone with no account. Stores the token's hash
    on the (already consumed) challenge row; the caller commits."""
    token = new_secret_token()
    challenge.registration_token_hash = hash_secret(token)
    challenge.registration_expires_at = now + REGISTRATION_TTL
    return RegistrationRequired(token, int(REGISTRATION_TTL.total_seconds()))


async def register(
    *,
    registration_token: str,
    display_name: str,
    device: DeviceInfo,
    session: AsyncSession,
    tokens: TokenIssuer,
    refresh: RefreshPolicy,
    now: datetime,
) -> LoggedIn:
    check_sms_platform(device)
    challenge = await repos.challenge_by_registration_hash_for_update(
        session, hash_secret(registration_token)
    )
    if (
        challenge is None
        or challenge.registration_expires_at is None
        or challenge.registration_expires_at <= now
    ):
        await session.rollback()
        log.info("registration token rejected")
        raise InvalidRegistrationToken

    # Read before a possible rollback: a rollback empties loaded objects (reading them again would
    # need a database round trip, which async SQLAlchemy refuses to do implicitly).
    phone = challenge.phone_e164
    masked = mask_phone(phone)

    # Single use: cleared in the same transaction that creates the user.
    challenge.registration_token_hash = None
    challenge.registration_expires_at = None
    try:
        user = await repos.insert_user(session, phone_e164=phone, display_name=display_name)
        user_id = user.id
        device_id = await add_primary(session, user_id, device, now)
        refresh_token = await issue_refresh_token(session, device_id, refresh, now)
        await session.commit()
    except IntegrityError:
        # phone_e164 is UNIQUE: someone registered this number first. Nothing was saved.
        await session.rollback()
        log.warning("register refused: already registered", extra={"phone": masked})
        raise AlreadyRegistered from None

    access_token, expires_in = tokens.issue(user_id=user_id, device_id=device_id)
    log.info(
        "user registered",
        extra={"user_id": str(user_id), "device_id": str(device_id), "phone": masked},
    )
    return LoggedIn(user_id, device_id, access_token, expires_in, refresh_token)
