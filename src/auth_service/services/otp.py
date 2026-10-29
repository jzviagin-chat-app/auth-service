"""One-time SMS codes: who may get one, creating it, storing only its hash, sending it.

No HTTP here (api/otp.py turns the exceptions below into responses) and no SQL (db/repos.py).
This module never reads `users`: a registered and an unknown number get exactly the same work,
so the answer (and its timing) can't reveal who has an account.
"""

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.config import Settings
from auth_service.core.masking import mask_phone
from auth_service.db import repos
from auth_service.services.rate_limit import (
    Limits,
    LimitsUnavailable,
    RateLimited,
    check_and_count,
)
from auth_service.services.sms import SmsSender, SmsSendError

log = logging.getLogger(__name__)

# Israeli mobile numbers only (an SMS cost guard): +972 5X XXX XXXX. [0-9], not \d: \d also
# matches other scripts' digits, e.g. Arabic-Indic ones.
ISRAELI_MOBILE = re.compile(r"\+9725[0-9]{8}")


@dataclass(frozen=True)
class OtpPolicy:
    ttl: timedelta
    limits: Limits
    daily_cap: int

    @classmethod
    def from_settings(cls, s: Settings) -> OtpPolicy:
        return cls(
            ttl=timedelta(seconds=s.otp_ttl_seconds),
            limits=Limits(
                cooldown_seconds=s.otp_resend_cooldown_seconds,
                phone_per_hour=s.otp_phone_max_per_hour,
                phone_per_day=s.otp_phone_max_per_day,
                ip_per_hour=s.otp_ip_max_per_hour,
                ip_per_day=s.otp_ip_max_per_day,
            ),
            daily_cap=s.sms_daily_cap,
        )


class InvalidPhone(Exception):
    """Not an Israeli mobile number in E.164 form."""


class SmsUnavailable(Exception):
    """We won't send a code now (daily cap, Redis down, provider failure). `reason`: for logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---- small pure helpers (unit-tested) ----


def is_israeli_mobile(phone: str) -> bool:
    return ISRAELI_MOBILE.fullmatch(phone) is not None


def new_code() -> str:
    """6 random digits from the OS's secure generator (`random` is predictable: not for secrets)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(pepper: bytes, phone: str, code: str) -> bytes:
    """What we store instead of the code: HMAC-SHA256(pepper, "phone:code"), 32 bytes.

    6 digits alone are easy to brute-force from a hash; the secret pepper (not in the database)
    makes a leaked table useless. Including the phone ties a code to its number.
    """
    return hmac.new(pepper, f"{phone}:{code}".encode(), hashlib.sha256).digest()


def phone_ref(pepper: bytes, phone: str) -> str:
    """A stable, non-reversible stand-in for a phone number (used in Redis keys)."""
    return hmac.new(pepper, f"rl:{phone}".encode(), hashlib.sha256).hexdigest()[:32]


def clean_ip(ip: str | None) -> str | None:
    """The client IP if it's a real address (stored in an `inet` column), else None."""
    if ip is None:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def sms_text(code: str, ttl: timedelta) -> str:
    # Placeholder wording until the provider and message template are chosen.
    return (
        f"{code} is your verification code. It expires in {int(ttl.total_seconds()) // 60} minutes."
    )


# ---- the use case ----


async def request_code(
    *,
    phone: str,
    ip: str | None,
    session: AsyncSession,
    redis: Redis,
    sms: SmsSender,
    pepper: bytes,
    policy: OtpPolicy,
    now: datetime,
) -> None:
    """Send a new code to `phone`, or raise InvalidPhone / RateLimited / SmsUnavailable."""
    if not is_israeli_mobile(phone):
        raise InvalidPhone
    masked = mask_phone(phone)
    ip = clean_ip(ip)
    log.info("otp requested", extra={"phone": masked, "ip": ip})

    # 1. Per-phone / per-IP limits (Redis).
    try:
        await check_and_count(redis, policy.limits, phone_ref(pepper, phone), ip or "unknown")
    except RateLimited as e:
        log.warning("otp rate limited", extra={"phone": masked, "ip": ip, "limit": e.limit})
        raise
    except LimitsUnavailable as e:
        log.error("rate limiter unavailable", extra={"error_type": str(e)})
        raise SmsUnavailable("rate_limiter_unavailable") from e

    # 2. Global daily cap + the challenge row, in one transaction.
    if not await repos.reserve_daily_sms(session, now.date(), policy.daily_cap):
        await session.rollback()
        log.error("sms daily cap reached", extra={"cap": policy.daily_cap})
        raise SmsUnavailable("daily_cap")
    code = new_code()
    await repos.insert_otp_challenge(
        session,
        phone_e164=phone,
        code_hash=hash_code(pepper, phone, code),
        expires_at=now + policy.ttl,
        request_ip=ip,
    )
    await session.commit()

    # 3. Send after the commit: a code that can't be verified must never reach a phone.
    try:
        message_id = await sms.send(phone, sms_text(code, policy.ttl))
    except SmsSendError as e:
        log.error(
            "sms send failed", extra={"phone": masked, "provider": sms.name, "status": e.status}
        )
        raise SmsUnavailable("send_failed") from e
    log.info("sms sent", extra={"phone": masked, "provider": sms.name, "message_id": message_id})
