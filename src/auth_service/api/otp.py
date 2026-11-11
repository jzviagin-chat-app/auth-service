"""SMS one-time codes (README.md, "API").

POST /auth/otp/request   {phone}                 -> send a code (rate limited); 202
POST /auth/otp/verify    {phone, code, device}   -> logged in, or registration required
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from auth_service.api.deps import DbSession, LoginContext, LoginContextDep, OtpContextDep
from auth_service.api.errors import ApiError
from auth_service.api.schemas import DeviceIn, LoggedInOut
from auth_service.services import devices, login, otp, registration
from auth_service.services.rate_limit import RateLimited

router = APIRouter(prefix="/otp", tags=["otp"])


class OtpRequestIn(BaseModel):
    # Checked in services/otp.py (Israeli mobile, E.164), so the error is our "invalid_phone".
    phone: str = Field(max_length=32, examples=["+972501234567"])


class OtpRequestOut(BaseModel):
    resend_after_seconds: int  # the app can enable "resend" after this (60, or longer: see README)
    expires_in_seconds: int


@router.post(
    "/request",
    status_code=202,
    responses={
        400: {"description": "invalid_phone"},
        429: {
            "description": "rate_limited: `limit` (resend_cooldown, phone_hour, phone_day, "
            "ip_hour, ip_day) and `retry_after_seconds` (also the Retry-After header)"
        },
        503: {"description": "sms_unavailable (Retry-After when the daily SMS cap is reached)"},
    },
)
async def request_otp(
    body: OtpRequestIn, request: Request, db: DbSession, ctx: OtpContextDep
) -> OtpRequestOut:
    """Send a code by SMS. The same 202 for every number, registered or not."""
    try:
        resend_after = await otp.request_code(
            phone=body.phone,
            ip=request.client.host if request.client else None,
            session=db,
            redis=ctx.redis,
            sms=ctx.sms,
            pepper=ctx.pepper,
            policy=ctx.policy,
            now=datetime.now(UTC),
        )
    except otp.InvalidPhone as e:
        raise ApiError(
            400, "invalid_phone", "Expected an Israeli mobile number like +972501234567"
        ) from e
    except RateLimited as e:
        raise ApiError(
            429,
            "rate_limited",
            "Too many code requests; try again later",
            headers={"Retry-After": str(e.retry_after)},
            extra={"limit": e.limit, "retry_after_seconds": e.retry_after},
        ) from e
    except otp.SmsUnavailable as e:
        headers = None if e.retry_after is None else {"Retry-After": str(e.retry_after)}
        raise ApiError(
            503, "sms_unavailable", "Can't send codes right now; try again later", headers=headers
        ) from e

    return OtpRequestOut(
        resend_after_seconds=resend_after,
        expires_in_seconds=int(ctx.policy.ttl.total_seconds()),
    )


class OtpVerifyIn(BaseModel):
    phone: str = Field(max_length=32, examples=["+972501234567"])
    code: str = Field(pattern=r"^[0-9]{6}$", examples=["123456"])
    device: DeviceIn


class RegistrationRequiredOut(BaseModel):
    status: Literal["registration_required"] = "registration_required"
    registration_token: str  # send it to POST /auth/register with a display name
    expires_in: int


@router.post(
    "/verify",
    responses={
        400: {
            "description": "invalid_phone, unsupported_platform, invalid_code (retry; "
            "`attempts_left`), too_many_attempts and code_expired (request a new one). The code "
            "errors add `resend_after_seconds` (0: now) when it can be read"
        },
        403: {"description": "account_blocked"},
        409: {"description": "login_conflict (a parallel login; retry)"},
        503: {"description": "tokens_unavailable"},
    },
)
async def verify_otp(
    body: OtpVerifyIn, db: DbSession, ctx: LoginContextDep
) -> LoggedInOut | RegistrationRequiredOut:
    """Check the code. Known number: logged in on this phone, which replaces the previous primary
    device. New number: a registration token for /register."""
    try:
        result = await login.verify_code(
            phone=body.phone,
            code=body.code,
            device=body.device.to_info(),
            session=db,
            pepper=ctx.pepper,
            tokens=ctx.tokens,
            refresh=ctx.refresh,
            revoked=ctx.revoked,
            now=datetime.now(UTC),
        )
    except otp.InvalidPhone as e:
        raise ApiError(
            400, "invalid_phone", "Expected an Israeli mobile number like +972501234567"
        ) from e
    except devices.UnsupportedPlatform as e:
        raise ApiError(
            400, "unsupported_platform", "SMS login is for phones only (platform ios or android)"
        ) from e
    except login.InvalidCode as e:
        extra = await _resend_after(ctx, body.phone)
        raise ApiError(
            400,
            "invalid_code",
            f"Wrong code; {e.attempts_left} attempts left",
            extra={"attempts_left": e.attempts_left, **extra},
        ) from e
    except login.TooManyAttempts as e:
        raise ApiError(
            400,
            "too_many_attempts",
            "Too many wrong tries for this code; request a new one",
            extra=await _resend_after(ctx, body.phone),
        ) from e
    except login.CodeExpired as e:
        raise ApiError(
            400,
            "code_expired",
            "This code can't be used anymore; request a new one",
            extra=await _resend_after(ctx, body.phone),
        ) from e
    except devices.AccountBlocked as e:
        raise ApiError(403, "account_blocked", "This account is blocked") from e
    except devices.LoginConflict as e:
        raise ApiError(
            409, "login_conflict", "Another login happened at the same time; retry"
        ) from e

    if isinstance(result, registration.RegistrationRequired):
        return RegistrationRequiredOut(
            registration_token=result.registration_token, expires_in=result.expires_in
        )
    return LoggedInOut.from_result(result)


async def _resend_after(ctx: LoginContext, phone: str) -> dict[str, int | str]:
    """{"resend_after_seconds": N} for the code-step errors, or {} if Redis can't tell."""
    seconds = await otp.resend_after(ctx.redis, ctx.pepper, phone)
    return {} if seconds is None else {"resend_after_seconds": seconds}
