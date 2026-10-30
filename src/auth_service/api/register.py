"""POST /auth/register {registration_token, display_name, device} -> a new user, logged in.

The registration token comes from POST /auth/otp/verify when the phone has no account yet.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from auth_service.api.deps import DbSession, LoginContextDep
from auth_service.api.errors import ApiError
from auth_service.api.schemas import DeviceIn, LoggedInOut
from auth_service.services import devices, registration

router = APIRouter(tags=["register"])

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class RegisterIn(BaseModel):
    registration_token: str = Field(max_length=100)
    display_name: DisplayName = Field(examples=["Jacob"])
    device: DeviceIn


@router.post(
    "/register",
    status_code=201,
    responses={
        400: {"description": "unsupported_platform"},
        401: {"description": "invalid_registration_token (verify the phone again)"},
        409: {"description": "already_registered (log in instead)"},
        503: {"description": "tokens_unavailable"},
    },
)
async def register(body: RegisterIn, db: DbSession, ctx: LoginContextDep) -> LoggedInOut:
    """Create the account for a verified phone number, plus this device, and log it in."""
    try:
        result = await registration.register(
            registration_token=body.registration_token,
            display_name=body.display_name,
            device=body.device.to_info(),
            session=db,
            tokens=ctx.tokens,
            refresh=ctx.refresh,
            now=datetime.now(UTC),
        )
    except devices.UnsupportedPlatform as e:
        raise ApiError(
            400, "unsupported_platform", "SMS login is for phones only (platform ios or android)"
        ) from e
    except registration.InvalidRegistrationToken as e:
        raise ApiError(
            401,
            "invalid_registration_token",
            "Registration expired or already done; verify your phone again",
        ) from e
    except registration.AlreadyRegistered as e:
        raise ApiError(409, "already_registered", "This number already has an account") from e
    return LoggedInOut.from_result(result)
