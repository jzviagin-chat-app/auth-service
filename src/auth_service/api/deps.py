"""FastAPI dependencies: values that routes receive as parameters (FastAPI's dependency injection).

Example:

    @router.post("/otp/request")
    async def request_otp(body: OtpRequestIn, db: DbSession, ctx: OtpContextDep) -> ...: ...

    @router.get("/devices")
    async def list_devices(me: CurrentDevice, db: DbSession) -> ...: ...   # a logged-in caller
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.api.errors import ApiError
from auth_service.config import get_settings
from auth_service.db import repos
from auth_service.db.engine import Database
from auth_service.services.keys import SigningKeys
from auth_service.services.otp import OtpPolicy
from auth_service.services.refresh import RefreshPolicy
from auth_service.services.revocation import RevokedDevices
from auth_service.services.sms import SmsSender
from auth_service.services.tokens import InvalidToken, TokenIssuer, verify_access_token


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
    revoked: RevokedDevices


def get_revoked(request: Request) -> RevokedDevices:
    """Publishes revoked devices for chat-service (does nothing without SHARED_REDIS_URL)."""
    revoked: RevokedDevices = request.app.state.revoked
    return revoked


RevokedDep = Annotated[RevokedDevices, Depends(get_revoked)]


def get_session_context(request: Request, revoked: RevokedDep) -> SessionContext:
    """503 before anything is read or changed when there is no signing key (JWT_PRIVATE_KEY)."""
    keys: SigningKeys | None = getattr(request.app.state, "keys", None)
    if keys is None:
        raise _tokens_unavailable()
    settings = get_settings()
    return SessionContext(
        tokens=TokenIssuer(keys, settings.jwt_issuer, settings.access_token_ttl_seconds),
        refresh=RefreshPolicy.from_settings(settings),
        revoked=revoked,
    )


SessionContextDep = Annotated[SessionContext, Depends(get_session_context)]


@dataclass(frozen=True)
class LoginContext:
    """What checking a code and logging in needs besides the database."""

    tokens: TokenIssuer
    refresh: RefreshPolicy
    revoked: RevokedDevices
    pepper: bytes


def get_login_context(ctx: SessionContextDep) -> LoginContext:
    """Also 503 (no code is used up) without the OTP_PEPPER to check codes with."""
    settings = get_settings()
    if settings.otp_pepper is None:
        raise _tokens_unavailable()
    return LoginContext(
        tokens=ctx.tokens,
        refresh=ctx.refresh,
        revoked=ctx.revoked,
        pepper=settings.otp_pepper.get_secret_value().encode(),
    )


LoginContextDep = Annotated[LoginContext, Depends(get_login_context)]


# ---- the logged-in caller (Authorization: Bearer <access token>) ----

# auto_error=False: a missing header gets our error shape (below), not FastAPI's. It also shows
# the "Authorize" button in the dev API docs.
_bearer = HTTPBearer(auto_error=False)
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


@dataclass(frozen=True)
class Caller:
    user_id: uuid.UUID
    device_id: uuid.UUID
    role: str  # "primary" | "linked" ("" when only the token was checked: TokenDevice)


def _invalid_token() -> ApiError:
    return ApiError(
        401,
        "invalid_token",
        "Missing, invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_token_device(request: Request, credentials: BearerDep) -> Caller:
    """The caller as the access token names it: signature, issuer and expiry checked, nothing
    else. Only for logout, which must also succeed for a device that is already signed out."""
    keys: SigningKeys | None = getattr(request.app.state, "keys", None)
    if keys is None:
        raise _tokens_unavailable()
    if credentials is None:
        raise _invalid_token()
    try:
        claims = verify_access_token(
            keys, credentials.credentials, issuer=get_settings().jwt_issuer
        )
    except InvalidToken as e:
        raise _invalid_token() from e
    return Caller(user_id=claims.user_id, device_id=claims.device_id, role="")


TokenDevice = Annotated[Caller, Depends(get_token_device)]


async def get_current_device(token: TokenDevice, db: DbSession) -> Caller:
    """The token, plus the device in Postgres: a signed-out device is refused at once (401), not
    only when its token expires. (auth never reads the revoked keys in Redis; those are for
    chat-service, which can't read our database.)"""
    found = await repos.device_with_status(db, token.device_id)
    if found is None or found[0].revoked_at is not None:
        raise _invalid_token()
    device, user_status = found
    if user_status != "active":
        raise ApiError(403, "account_blocked", "This account is blocked")
    return Caller(user_id=device.user_id, device_id=device.id, role=device.role)


CurrentDevice = Annotated[Caller, Depends(get_current_device)]
