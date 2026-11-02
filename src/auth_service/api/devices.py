"""Devices of the signed-in user (README.md, "API"). WhatsApp model: only the primary manages them.

GET    /auth/devices              -> my active devices
DELETE /auth/devices/{device_id}  -> sign one of my linked devices out
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel

from auth_service.api.deps import CurrentDevice, DbSession, RevokedDep
from auth_service.api.errors import ApiError
from auth_service.services import devices

router = APIRouter(prefix="/devices", tags=["devices"])

_NOT_ALLOWED = (
    "Only the primary device manages devices, and it can't remove itself (log out instead)"
)


class DeviceOut(BaseModel):
    id: uuid.UUID
    platform: str  # "ios" | "android" | "web"
    name: str | None
    role: str  # "primary" | "linked"
    created_at: datetime  # when it logged in
    last_seen_at: datetime | None  # last login or token refresh (not chat presence)
    current: bool  # the device making this request


class DevicesOut(BaseModel):
    devices: list[DeviceOut]


@router.get(
    "",
    responses={
        401: {"description": "invalid_token"},
        403: {"description": "action_not_allowed (not the primary) / account_blocked"},
    },
)
async def list_devices(me: CurrentDevice, db: DbSession) -> DevicesOut:
    try:
        found = await devices.list_devices(
            db, user_id=me.user_id, caller_device_id=me.device_id, caller_role=me.role
        )
    except devices.ActionNotAllowed as e:
        raise ApiError(403, "action_not_allowed", _NOT_ALLOWED) from e
    return DevicesOut(devices=[DeviceOut(**vars(d)) for d in found])


@router.delete(
    "/{device_id}",
    status_code=204,
    responses={
        401: {"description": "invalid_token"},
        403: {"description": "action_not_allowed (not the primary, or itself) / account_blocked"},
        404: {"description": "not_found"},
    },
)
async def remove_device(
    device_id: uuid.UUID, me: CurrentDevice, db: DbSession, revoked: RevokedDep
) -> Response:
    """Sign out one of my linked devices. Already signed out: also 204."""
    try:
        await devices.remove_device(
            db,
            user_id=me.user_id,
            caller_device_id=me.device_id,
            caller_role=me.role,
            device_id=device_id,
            revoked=revoked,
            now=datetime.now(UTC),
        )
    except devices.ActionNotAllowed as e:
        raise ApiError(403, "action_not_allowed", _NOT_ALLOWED) from e
    except devices.DeviceNotFound as e:
        raise ApiError(404, "not_found", "No such device") from e
    return Response(status_code=204)
