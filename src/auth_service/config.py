"""Settings, read from environment variables (and from a local `.env` file in development).

Every field maps to an env var with the same name in upper case: `database_url` <- DATABASE_URL.
Pydantic validates the types at startup, so a bad value fails fast instead of mid-request.
"""

from functools import lru_cache
from typing import Literal

from pydantic import PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Which SMS sender to use. Only "fake" exists for now (logs instead of sending, costs nothing).
    sms_provider: Literal["fake"] = "fake"
    # Global money guard: at most this many SMS per UTC day, across all pods (counted in Postgres).
    sms_daily_cap: PositiveInt = 100


@lru_cache
def get_settings() -> Settings:
    """One Settings object per process (read once, reused everywhere)."""
    return Settings()
