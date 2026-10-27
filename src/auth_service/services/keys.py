"""Signing keys for access tokens (Ed25519, JWT algorithm "EdDSA") and the public JWKS document.

The private key signs; anyone can check a signature with the public key, which we publish at
/auth/.well-known/jwks.json. Other services (chat-service first) fetch it once and verify tokens
locally, without calling auth-service.

Each key's id (`kid`, in every token's header) is its RFC 7638 thumbprint: a hash of the public key.
So a new key automatically gets a new id, with nothing to configure.

Rotation: put the new private key in JWT_PRIVATE_KEY and the old *public* key in
JWT_PREVIOUS_PUBLIC_KEY. Both are published, so tokens signed with the old key keep working until
they expire (15 min). After that (plus consumers' cache time), remove JWT_PREVIOUS_PUBLIC_KEY.
"""

import base64
import hashlib
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from auth_service.config import Settings

log = logging.getLogger(__name__)


class InvalidKeyConfig(Exception):
    """A configured key can't be used. The message never contains key material."""


def _b64url(data: bytes) -> str:
    """Base64url without padding, as JWT/JWK use it."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_jwk(public_key: Ed25519PublicKey) -> dict[str, str]:
    """The public key as a JWK (RFC 8037): key type OKP, curve Ed25519, x = the 32 raw bytes."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}


def key_id(public_key: Ed25519PublicKey) -> str:
    """RFC 7638 thumbprint: SHA-256 of the key's required members as compact, sorted JSON."""
    canonical = json.dumps(public_jwk(public_key), separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode()).digest())


class SigningKeys:
    """The current private key (signs new tokens) plus every public key that tokens may carry."""

    def __init__(self, private_key: Ed25519PrivateKey, previous: Ed25519PublicKey | None = None):
        self.private_key = private_key
        self.kid = key_id(private_key.public_key())
        self._public: dict[str, Ed25519PublicKey] = {self.kid: private_key.public_key()}
        if previous is not None:
            self._public.setdefault(key_id(previous), previous)

    def public_key(self, kid: str) -> Ed25519PublicKey | None:
        return self._public.get(kid)

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """The document served at /auth/.well-known/jwks.json (public keys only)."""
        return {
            "keys": [
                {**public_jwk(key), "kid": kid, "alg": "EdDSA", "use": "sig"}
                for kid, key in self._public.items()
            ]
        }


def load_private_key(pem: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError:
        raise InvalidKeyConfig("JWT_PRIVATE_KEY is not a valid PEM private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise InvalidKeyConfig("JWT_PRIVATE_KEY must be an Ed25519 key")
    return key


def load_public_key(pem: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem.encode())
    except ValueError:
        raise InvalidKeyConfig("JWT_PREVIOUS_PUBLIC_KEY is not a valid PEM public key") from None
    if not isinstance(key, Ed25519PublicKey):
        raise InvalidKeyConfig("JWT_PREVIOUS_PUBLIC_KEY must be an Ed25519 key")
    return key


def make_signing_keys(settings: Settings) -> SigningKeys | None:
    """Called once at startup. None = no key configured (prod): token endpoints answer 503.
    A key that is set but broken raises InvalidKeyConfig, so the pod fails to start (visible)."""
    pem = settings.jwt_private_key.get_secret_value().strip() if settings.jwt_private_key else ""
    if not pem:
        if settings.environment == "dev":
            log.warning("JWT_PRIVATE_KEY not set: throwaway signing key (dev only)")
            return SigningKeys(Ed25519PrivateKey.generate())
        log.error("JWT_PRIVATE_KEY not set: no access tokens; jwks.json answers 503")
        return None
    previous_pem = (settings.jwt_previous_public_key or "").strip()
    keys = SigningKeys(
        load_private_key(pem), load_public_key(previous_pem) if previous_pem else None
    )
    published = len(keys.jwks()["keys"])
    log.info("signing key loaded", extra={"kid": keys.kid, "published_keys": published})
    return keys
