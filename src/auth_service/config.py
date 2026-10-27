"""Settings, read from environment variables (and from a local `.env` file in development).

Every field maps to an env var with the same name in upper case: `database_url` <- DATABASE_URL.
Pydantic validates the types at startup, so a bad value fails fast instead of mid-request.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---- SMS providers: one settings block per provider, each with its own env var prefix ----
#
# Plain values (sender, cap, timeout) have their defaults here: changing them is an auth-service
# release. Secret fields only *point to* an env var, which Kubernetes fills from a sealed Secret
# (chat-infra); the comment on each secret field names that Secret. No secret values in code.


class FakeSmsSettings(BaseSettings):
    """Provider "fake": sends nothing, costs nothing (development, tests, CI). Env: FAKE_SMS_*."""

    model_config = SettingsConfigDict(env_prefix="FAKE_SMS_", env_file=".env", extra="ignore")

    daily_cap: PositiveInt = 100  # FAKE_SMS_DAILY_CAP


class GlobalSmsSettings(BaseSettings):
    """Provider "globalsms": Global SMS (itnewsletter.co.il). Env: GLOBALSMS_*."""

    model_config = SettingsConfigDict(env_prefix="GLOBALSMS_", env_file=".env", extra="ignore")

    # GLOBALSMS_API_KEY <- sealed Secret "auth-sms" (k8s/auth.yaml in chat-infra).
    api_key: SecretStr | None = None
    # "sapi" is their host without an IP allow-list (Global SMS's advice, verified 2026-10-26).
    base_url: str = "https://sapi.itnewsletter.co.il"
    # Sender shown to recipients: pre-approved in the Global SMS account (max 11 characters).
    originator: str | None = Field(default="ZviaginChat", max_length=11)
    daily_cap: PositiveInt = 100  # GLOBALSMS_DAILY_CAP
    timeout_seconds: PositiveFloat = 5  # the whole request, so a slow provider can't hang requests


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "auth-service"
    # "prod" by default, so a forgotten setting never exposes dev-only features (like /auth/docs).
    environment: Literal["dev", "prod"] = "prod"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    pod_name: str = "local"  # set by Kubernetes (k8s/auth.yaml)

    # postgresql://user:password@host:5432/db (the Secret "auth-db" provides it in the cluster).
    # Optional until the first handler needs the database.
    database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Addresses allowed to tell us the client's IP in X-Forwarded-For: only the HAProxy ingress,
    # which runs inside the k3s pod network. Anyone else's X-Forwarded-For is ignored.
    # As an env var it is JSON: TRUSTED_PROXIES='["10.42.0.0/16"]'
    trusted_proxies: list[str] = ["10.42.0.0/16"]

    # redis://redis:6379/1 in the cluster (db 1 is auth's; chat uses db 0). Holds the per-phone /
    # per-IP rate-limit counters. Unset or unreachable -> /auth/otp/request answers 503
    # (fail closed: no codes without limits).
    redis_url: str | None = None

    # Secret key mixed into every OTP hash (Secret "auth-otp"). SecretStr hides it from repr/logs.
    # Unset -> /auth/otp/request answers 503.
    otp_pepper: SecretStr | None = None
    otp_ttl_seconds: PositiveInt = 300  # a code is valid for 5 minutes
    otp_resend_cooldown_seconds: PositiveInt = 60
    otp_phone_max_per_hour: PositiveInt = 5
    otp_phone_max_per_day: PositiveInt = 10
    # Per IP: generous, because mobile carriers put many users behind one IP (carrier-grade NAT).
    otp_ip_max_per_hour: PositiveInt = 30
    otp_ip_max_per_day: PositiveInt = 200
    otp_retention_hours: PositiveInt = 24  # older otp_challenges rows are deleted
    otp_cleanup_interval_seconds: PositiveInt = 3600

    # ---- access tokens (JWT signed with Ed25519 / "EdDSA") ----
    # PEM of the private key that signs access tokens. JWT_PRIVATE_KEY <- sealed Secret "auth-jwt".
    # Unset: prod issues no tokens (503); ENVIRONMENT=dev uses a throwaway key made at startup.
    jwt_private_key: SecretStr | None = None
    # PEM of the previous public key, only during a key rotation (JWT_PREVIOUS_PUBLIC_KEY, same
    # Secret): tokens signed with the old key stay valid until they expire.
    jwt_previous_public_key: str | None = None
    jwt_issuer: str = "auth-service"  # the "iss" claim; consumers check it
    access_token_ttl_seconds: PositiveInt = 900  # 15 minutes

    # Which SMS provider sends the codes. Each one has its own settings block (above).
    sms_provider: Literal["fake", "globalsms"] = "fake"
    fake_sms: FakeSmsSettings = Field(default_factory=FakeSmsSettings)
    globalsms: GlobalSmsSettings = Field(default_factory=GlobalSmsSettings)

    @property
    def sms_daily_cap(self) -> int:
        """Global money guard: max SMS per UTC day, all pods together (counted in Postgres).
        The active provider's cap; the count itself is shared (one row per day)."""
        match self.sms_provider:
            case "fake":
                return self.fake_sms.daily_cap
            case "globalsms":
                return self.globalsms.daily_cap


@lru_cache
def get_settings() -> Settings:
    """One Settings object per process (read once, reused everywhere)."""
    return Settings()
