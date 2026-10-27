"""Signing keys, access tokens and the JWKS endpoint (no database)."""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import OKPAlgorithm
from pydantic import SecretStr

from auth_service.config import Settings
from auth_service.main import create_app
from auth_service.services.keys import InvalidKeyConfig, SigningKeys, key_id, make_signing_keys
from auth_service.services.tokens import (
    InvalidToken,
    issue_access_token,
    verify_access_token,
)

ISSUER = "auth-service"
USER = uuid.uuid7()
DEVICE = uuid.uuid7()


def private_pem(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_pem(key: Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def b64url(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


@pytest.fixture
def keys() -> SigningKeys:
    return SigningKeys(Ed25519PrivateKey.generate())


def issue(keys: SigningKeys, now: datetime | None = None) -> str:
    token, _ = issue_access_token(
        keys, user_id=USER, device_id=DEVICE, issuer=ISSUER, ttl_seconds=900, now=now
    )
    return token


# ---- issuing ----


def test_token_follows_the_contract(keys: SigningKeys) -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # in the past: a future `iat` is rejected
    token, expires_in = issue_access_token(
        keys, user_id=USER, device_id=DEVICE, issuer=ISSUER, ttl_seconds=900, now=now
    )
    assert expires_in == 900
    assert jwt.get_unverified_header(token) == {"alg": "EdDSA", "kid": keys.kid, "typ": "JWT"}

    # Verify the way another service would: with the public key from the JWKS document.
    [jwk] = keys.jwks()["keys"]
    public_key = OKPAlgorithm.from_jwk(json.dumps(jwk))
    claims = jwt.decode(token, public_key, algorithms=["EdDSA"], options={"verify_exp": False})
    assert claims["iss"] == ISSUER
    assert claims["sub"] == str(USER)
    assert claims["did"] == str(DEVICE)
    assert claims["iat"] == int(now.timestamp())
    assert claims["exp"] == claims["iat"] + 900
    assert uuid.UUID(claims["jti"]).version == 7


def test_every_token_has_its_own_id(keys: SigningKeys) -> None:
    assert (
        jwt.decode(issue(keys), options={"verify_signature": False})["jti"]
        != jwt.decode(issue(keys), options={"verify_signature": False})["jti"]
    )


# ---- verifying ----


def test_valid_token(keys: SigningKeys) -> None:
    claims = verify_access_token(keys, issue(keys), issuer=ISSUER)
    assert (claims.user_id, claims.device_id) == (USER, DEVICE)


def test_expired(keys: SigningKeys) -> None:
    old = datetime.now(UTC) - timedelta(minutes=20)
    with pytest.raises(InvalidToken, match="expired"):
        verify_access_token(keys, issue(keys, now=old), issuer=ISSUER)


def test_small_clock_difference_is_tolerated(keys: SigningKeys) -> None:
    almost = datetime.now(UTC) - timedelta(seconds=900 + 10)  # expired 10 s ago, leeway 30 s
    verify_access_token(keys, issue(keys, now=almost), issuer=ISSUER)


def test_tampered(keys: SigningKeys) -> None:
    header, payload, signature = issue(keys).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["sub"] = str(uuid.uuid7())  # pretend to be someone else
    with pytest.raises(InvalidToken, match="invalid"):
        verify_access_token(keys, f"{header}.{b64url(claims)}.{signature}", issuer=ISSUER)


def test_wrong_issuer(keys: SigningKeys) -> None:
    token, _ = issue_access_token(
        keys, user_id=USER, device_id=DEVICE, issuer="someone-else", ttl_seconds=900
    )
    with pytest.raises(InvalidToken, match="invalid"):
        verify_access_token(keys, token, issuer=ISSUER)


def test_signed_by_another_key(keys: SigningKeys) -> None:
    other = SigningKeys(Ed25519PrivateKey.generate())
    with pytest.raises(InvalidToken, match="unknown_kid"):
        verify_access_token(keys, issue(other), issuer=ISSUER)


def test_alg_none_is_rejected(keys: SigningKeys) -> None:
    claims = jwt.decode(issue(keys), options={"verify_signature": False})
    unsigned = f"{b64url({'alg': 'none', 'kid': keys.kid})}.{b64url(claims)}."
    with pytest.raises(InvalidToken, match="bad_algorithm"):
        verify_access_token(keys, unsigned, issuer=ISSUER)


def test_hs256_with_public_key_as_secret_is_rejected(keys: SigningKeys) -> None:
    # The classic confusion attack: sign with HMAC using the *public* key as the secret.
    claims = jwt.decode(issue(keys), options={"verify_signature": False})
    x = keys.jwks()["keys"][0]["x"].encode()
    forged = jwt.encode(claims, x, algorithm="HS256", headers={"kid": keys.kid})
    with pytest.raises(InvalidToken, match="bad_algorithm"):
        verify_access_token(keys, forged, issuer=ISSUER)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "eyJhbGciOiJFZERTQSJ9.e30."])
def test_malformed(keys: SigningKeys, garbage: str) -> None:
    with pytest.raises(InvalidToken):
        verify_access_token(keys, garbage, issuer=ISSUER)


def test_missing_claim(keys: SigningKeys) -> None:
    no_device = jwt.encode(
        {"iss": ISSUER, "sub": str(USER), "iat": 1, "exp": 4102444800, "jti": "x"},
        keys.private_key,
        algorithm="EdDSA",
        headers={"kid": keys.kid},
    )
    with pytest.raises(InvalidToken, match="invalid"):
        verify_access_token(keys, no_device, issuer=ISSUER)


# ---- rotation ----


def test_rotation_keeps_old_tokens_valid() -> None:
    old_key = Ed25519PrivateKey.generate()
    old_token = issue(SigningKeys(old_key))
    rotated = SigningKeys(Ed25519PrivateKey.generate(), previous=old_key.public_key())

    assert len(rotated.jwks()["keys"]) == 2  # both published
    verify_access_token(rotated, old_token, issuer=ISSUER)  # old token still accepted
    assert jwt.get_unverified_header(issue(rotated))["kid"] == rotated.kid  # new tokens: new key

    after_rotation = SigningKeys(rotated.private_key)  # previous key removed
    with pytest.raises(InvalidToken, match="unknown_kid"):
        verify_access_token(after_rotation, old_token, issuer=ISSUER)


# ---- key ids and loading ----


def test_kid_is_the_key_thumbprint() -> None:
    key = Ed25519PrivateKey.generate()
    assert key_id(key.public_key()) == key_id(key.public_key())
    assert key_id(key.public_key()) != key_id(Ed25519PrivateKey.generate().public_key())


def test_kid_matches_rfc_8037_example() -> None:
    # RFC 8037, appendix A.3: the thumbprint of the example Ed25519 public key.
    jwk = '{"kty":"OKP","crv":"Ed25519","x":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}'
    public_key = OKPAlgorithm.from_jwk(jwk)
    assert key_id(public_key) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"  # type: ignore[arg-type]


def test_jwks_has_no_private_material(keys: SigningKeys) -> None:
    [jwk] = keys.jwks()["keys"]
    assert jwk.keys() == {"kty", "crv", "x", "kid", "alg", "use"}
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519" and jwk["alg"] == "EdDSA"


def test_load_from_pem_settings() -> None:
    current, previous = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = Settings(
        environment="prod",
        jwt_private_key=SecretStr(private_pem(current)),
        jwt_previous_public_key=public_pem(previous),
    )
    keys = make_signing_keys(settings)
    assert keys is not None
    assert keys.kid == key_id(current.public_key())
    assert {k["kid"] for k in keys.jwks()["keys"]} == {
        key_id(current.public_key()),
        key_id(previous.public_key()),
    }


def test_broken_key_fails_startup() -> None:
    with pytest.raises(InvalidKeyConfig, match="JWT_PRIVATE_KEY"):
        make_signing_keys(Settings(jwt_private_key=SecretStr("not a key")))


def test_broken_key_error_has_no_key_material() -> None:
    # Cut the key short (changing one character would just make another valid Ed25519 key:
    # any 32 random bytes are one).
    begin, body, end = private_pem(Ed25519PrivateKey.generate()).splitlines()
    truncated = f"{begin}\n{body[:24]}\n{end}\n"
    with pytest.raises(InvalidKeyConfig) as err:
        make_signing_keys(Settings(jwt_private_key=SecretStr(truncated)))
    assert body[:24] not in str(err.value)
    assert err.value.__suppress_context__  # cryptography's own error isn't chained


def test_no_key_in_prod() -> None:
    assert make_signing_keys(Settings(environment="prod", jwt_private_key=None)) is None


def test_no_key_in_dev_uses_a_throwaway_key() -> None:
    keys = make_signing_keys(Settings(environment="dev", jwt_private_key=None))
    assert keys is not None
    verify_access_token(keys, issue(keys), issuer=ISSUER)


# ---- the endpoint ----


@pytest.fixture
async def app(prod_settings: None) -> AsyncIterator[FastAPI]:
    yield create_app()


async def get_jwks(app: FastAPI) -> tuple[int, dict[str, object], str | None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/.well-known/jwks.json")
    return r.status_code, r.json(), r.headers.get("cache-control")


async def test_jwks_endpoint(app: FastAPI, keys: SigningKeys) -> None:
    app.state.keys = keys
    status, body, cache = await get_jwks(app)
    assert status == 200
    assert body == keys.jwks()
    assert cache == "public, max-age=300"


async def test_jwks_endpoint_without_keys(app: FastAPI) -> None:
    app.state.keys = None
    status, body, _ = await get_jwks(app)
    assert status == 503
    assert body["error"] == "keys_unavailable"
