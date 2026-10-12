"""Devices of the signed-in user. Planned endpoints (README.md, "API"):

GET    /auth/devices              -> list my devices
DELETE /auth/devices/{device_id}  -> sign that device out (revoke its refresh tokens)
"""

from fastapi import APIRouter

router = APIRouter(prefix="/devices", tags=["devices"])
