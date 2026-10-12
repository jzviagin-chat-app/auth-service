"""Session tokens. Planned endpoints (README.md, "API"):

POST /auth/token/refresh {refresh_token}         -> new access + refresh pair (rotation)
POST /auth/logout        (access token)          -> revoke this device's refresh tokens
"""

from fastapi import APIRouter

router = APIRouter(tags=["tokens"])
