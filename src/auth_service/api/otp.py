"""SMS one-time codes (README.md, "API").

POST /auth/otp/request   {phone}                 -> send a code (rate limited); 202
POST /auth/otp/verify    {phone, code, device}   -> find/create user + device, tokens (step 4)
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from auth_service.api.deps import DbSession, OtpContextDep
from auth_service.api.errors import ApiError
from auth_service.services import otp
from auth_service.services.rate_limit import RateLimited

router = APIRouter(prefix="/otp", tags=["otp"])


class OtpRequestIn(BaseModel):
    # Checked in services/otp.py (Israeli mobile, E.164), so the error is our "invalid_phone".
    phone: str = Field(max_length=32, examples=["+972501234567"])


class OtpRequestOut(BaseModel):
    resend_after_seconds: int  # the app can enable "resend" after this
    expires_in_seconds: int


@router.post(
    "/request",
    status_code=202,
    responses={
        400: {"description": "invalid_phone"},
        429: {"description": "rate_limited (see Retry-After)"},
        503: {"description": "sms_unavailable"},
    },
)
async def request_otp(
    body: OtpRequestIn, request: Request, db: DbSession, ctx: OtpContextDep
) -> OtpRequestOut:
    """Send a code by SMS. The same 202 for every number, registered or not."""
    try:
        await otp.request_code(
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
        ) from e
    except otp.SmsUnavailable as e:
        raise ApiError(503, "sms_unavailable", "Can't send codes right now; try again later") from e

    return OtpRequestOut(
        resend_after_seconds=ctx.policy.limits.cooldown_seconds,
        expires_in_seconds=int(ctx.policy.ttl.total_seconds()),
    )
