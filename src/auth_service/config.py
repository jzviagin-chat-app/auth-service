"""Settings, read from environment variables (and from a local `.env` file in development).

Every field maps to an env var with the same name in upper case: `database_url` <- DATABASE_URL.
Pydantic validates the types at startup, so a bad value fails fast instead of mid-request.
"""

from functools import lru_cache
from typing import Literal

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


@lru_cache
def get_settings() -> Settings:
    """One Settings object per process (read once, reused everywhere)."""
    return Settings()
