"""Public signing keys. Planned endpoint (README.md, "API"):

GET /auth/.well-known/jwks.json  -> public keys other services use to verify access tokens
"""

from fastapi import APIRouter

router = APIRouter(tags=["jwks"])
