"""Session tokens (README.md, "API").

POST /auth/token/refresh {refresh_token}         -> new access + refresh tokens (rotation)
POST /auth/logout        (access token)          -> sign this device out (step 6b)
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from auth_service.api.deps import DbSession, SessionContextDep
from auth_service.api.errors import ApiError
from auth_service.api.schemas import LoggedInOut
from auth_service.services import devices, refresh

router = APIRouter(tags=["tokens"])


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=100)


@router.post(
    "/token/refresh",
    responses={
        401: {"description": "invalid_refresh_token (log in again by SMS)"},
        403: {"description": "account_blocked"},
        503: {"description": "tokens_unavailable"},
    },
)
async def refresh_tokens(body: RefreshIn, db: DbSession, ctx: SessionContextDep) -> LoggedInOut:
    """Swap the refresh token for a new one plus a new access token. If the reply gets lost, send
    the same refresh token again: that's safe, after any delay."""
    try:
        result = await refresh.refresh(
            refresh_token=body.refresh_token,
            session=db,
            tokens=ctx.tokens,
            policy=ctx.refresh,
            now=datetime.now(UTC),
        )
    except refresh.InvalidRefreshToken as e:
        # One code for every reason (unknown, expired, reused, signed out): the app does the same
        # thing in each case. The reason is in our logs.
        raise ApiError(401, "invalid_refresh_token", "This session has ended; log in again") from e
    except devices.AccountBlocked as e:
        raise ApiError(403, "account_blocked", "This account is blocked") from e
    return LoggedInOut.from_result(result)
