"""Random tokens that are stored only as a hash (registration tokens, refresh tokens)."""

import hashlib
import secrets


def new_secret_token() -> str:
    """256 random bits, URL-safe text (≈ crypto.randomBytes(32).toString("base64url") in Node)."""
    return secrets.token_urlsafe(32)


def hash_secret(token: str) -> bytes:
    """Stored instead of the token, so a database leak hands out nothing usable. Plain SHA-256 is
    enough: the token is 256 random bits, so (unlike a 6-digit code) it can't be guessed from its
    hash."""
    return hashlib.sha256(token.encode()).digest()
