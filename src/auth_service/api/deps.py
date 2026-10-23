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
from auth_service.services.otp import OtpPolicy
from auth_service.services.sms import SmsSender


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
