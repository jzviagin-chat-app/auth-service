"""FastAPI dependencies: values that routes receive as parameters (FastAPI's dependency injection).

Example:

    @router.post("/otp/request")
    async def request_otp(body: OtpRequestIn, db: DbSession, ctx: OtpContextDep) -> ...: ...
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.api.errors import ApiError
from auth_service.config import get_settings
from auth_service.db.engine import Database
from auth_service.services.keys import SigningKeys
from auth_service.services.otp import OtpPolicy
from auth_service.services.refresh import RefreshPolicy
from auth_service.services.sms import SmsSender
from auth_service.services.tokens import TokenIssuer


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request; rolled back automatically if the handler raises."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database not configured")
    async with db.session() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@dataclass(frozen=True)
class OtpContext:
    """Everything sending a code needs besides the database."""

    redis: Redis
    sms: SmsSender
    pepper: bytes
    policy: OtpPolicy


def get_otp_context(request: Request) -> OtpContext:
    """503 when Redis or the pepper isn't configured: never send codes we can't limit or check."""
    redis: Redis | None = getattr(request.app.state, "redis", None)
    sms: SmsSender | None = getattr(request.app.state, "sms", None)
    settings = get_settings()
    if redis is None or sms is None or settings.otp_pepper is None:
        raise ApiError(503, "sms_unavailable", "Can't send codes right now; try again later")
    return OtpContext(
        redis=redis,
        sms=sms,
        pepper=settings.otp_pepper.get_secret_value().encode(),
        policy=OtpPolicy.from_settings(settings),
    )


OtpContextDep = Annotated[OtpContext, Depends(get_otp_context)]


def _tokens_unavailable() -> ApiError:
    return ApiError(503, "tokens_unavailable", "Can't log in right now; try again later")


@dataclass(frozen=True)
class SessionContext:
    """What issuing access and refresh tokens needs besides the database (token/refresh)."""

    tokens: TokenIssuer
    refresh: RefreshPolicy


def get_session_context(request: Request) -> SessionContext:
    """503 before anything is read or changed when there is no signing key (JWT_PRIVATE_KEY)."""
    keys: SigningKeys | None = getattr(request.app.state, "keys", None)
    if keys is None:
        raise _tokens_unavailable()
    settings = get_settings()
    return SessionContext(
        tokens=TokenIssuer(keys, settings.jwt_issuer, settings.access_token_ttl_seconds),
        refresh=RefreshPolicy.from_settings(settings),
    )


SessionContextDep = Annotated[SessionContext, Depends(get_session_context)]


@dataclass(frozen=True)
class LoginContext:
    """What checking a code and logging in needs besides the database."""

    tokens: TokenIssuer
    refresh: RefreshPolicy
    pepper: bytes


def get_login_context(ctx: SessionContextDep) -> LoginContext:
    """Also 503 (no code is used up) without the OTP_PEPPER to check codes with."""
    settings = get_settings()
    if settings.otp_pepper is None:
        raise _tokens_unavailable()
    return LoginContext(
        tokens=ctx.tokens,
        refresh=ctx.refresh,
        pepper=settings.otp_pepper.get_secret_value().encode(),
    )


LoginContextDep = Annotated[LoginContext, Depends(get_login_context)]
