"""Access tokens: short-lived JWTs that tell any service which user and device is calling.

Contract (ARCHITECTURE.md §5.1): signed with EdDSA (Ed25519), valid 15 minutes, header `kid`, claims
  iss = "auth-service", sub = user id, did = device id, iat, exp, jti (a unique id per token).
A JWT is readable by anyone (base64, not encrypted): never put secrets in it. The signature only
proves auth-service issued it and nothing was changed.

Refresh tokens (long-lived, opaque, stored as hashes) are in services/refresh.py.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt

from auth_service.services.keys import SigningKeys

ALGORITHM = "EdDSA"
_REQUIRED_CLAIMS = ["iss", "sub", "did", "iat", "exp", "jti"]


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    device_id: uuid.UUID
    token_id: str
    expires_at: datetime


class InvalidToken(Exception):
    """The token can't be trusted. `reason` is safe to log (never the token itself)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def issue_access_token(
    keys: SigningKeys,
    *,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    issuer: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Sign a new access token; returns (token, seconds until it expires)."""
    issued_at = int((now or datetime.now(UTC)).timestamp())
    claims = {
        "iss": issuer,
        "sub": str(user_id),
        "did": str(device_id),
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": str(uuid.uuid7()),
    }
    token = jwt.encode(claims, keys.private_key, algorithm=ALGORITHM, headers={"kid": keys.kid})
    return token, ttl_seconds


def verify_access_token(
    keys: SigningKeys, token: str, *, issuer: str, leeway_seconds: int = 30
) -> AccessClaims:
    """Check signature, issuer and expiry; raise InvalidToken otherwise.

    The algorithm is fixed to EdDSA and the key is chosen by `kid` from *our* key list, so a token
    can't pick its own algorithm (the classic `alg: none` / HS256 tricks) or bring its own key.
    `leeway_seconds` tolerates small clock differences between machines.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise InvalidToken("malformed") from None
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("bad_algorithm")
    kid = header.get("kid")
    public_key = keys.public_key(kid) if isinstance(kid, str) else None
    if public_key is None:
        raise InvalidToken("unknown_kid")
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            issuer=issuer,
            leeway=leeway_seconds,
            options={"require": _REQUIRED_CLAIMS},
        )
        return AccessClaims(
            user_id=uuid.UUID(claims["sub"]),
            device_id=uuid.UUID(claims["did"]),
            token_id=str(claims["jti"]),
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
        )
    except jwt.ExpiredSignatureError:
        raise InvalidToken("expired") from None
    except jwt.InvalidTokenError, ValueError, TypeError:
        raise InvalidToken("invalid") from None


@dataclass(frozen=True)
class TokenIssuer:
    """Everything issuing an access token needs, bundled (built from settings per request)."""

    keys: SigningKeys
    issuer: str
    ttl_seconds: int

    def issue(self, *, user_id: uuid.UUID, device_id: uuid.UUID) -> tuple[str, int]:
        return issue_access_token(
            self.keys,
            user_id=user_id,
            device_id=device_id,
            issuer=self.issuer,
            ttl_seconds=self.ttl_seconds,
        )
