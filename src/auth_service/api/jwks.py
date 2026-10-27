"""Public signing keys (README.md, "API").

GET /auth/.well-known/jwks.json  -> public keys other services use to verify access tokens.
In the cluster: http://auth:8000/auth/.well-known/jwks.json. Consumers cache it and refetch once
when a token carries an unknown `kid` (ARCHITECTURE.md §5.1).
"""

from fastapi import APIRouter, Request, Response

from auth_service.api.errors import ApiError
from auth_service.services.keys import SigningKeys

router = APIRouter(tags=["jwks"])


@router.get("/.well-known/jwks.json")
async def jwks(request: Request, response: Response) -> dict[str, list[dict[str, str]]]:
    keys: SigningKeys | None = getattr(request.app.state, "keys", None)
    if keys is None:
        raise ApiError(503, "keys_unavailable", "Signing keys are not configured")
    # Public data: caches (and consumers) may keep it for 5 minutes.
    response.headers["Cache-Control"] = "public, max-age=300"
    return keys.jwks()
