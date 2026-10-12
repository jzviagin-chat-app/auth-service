"""App factory and lifecycle. Start with: uv run uvicorn auth_service.main:app --reload"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from auth_service.api import health
from auth_service.api.router import api_router
from auth_service.config import get_settings
from auth_service.core.logging import setup_logging
from auth_service.db.engine import Database

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Runs once per process: code before `yield` at startup, after `yield` at shutdown."""
    settings = get_settings()
    app.state.db = None
    if settings.database_url:
        app.state.db = Database(
            settings.database_url, settings.db_pool_size, settings.db_max_overflow
        )
    else:
        log.warning("DATABASE_URL not set: database features are disabled")
    log.info("started", extra={"environment": settings.environment})
    yield
    if app.state.db is not None:
        await app.state.db.dispose()
    log.info("stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, settings.service_name, settings.pod_name)
    dev = settings.environment == "dev"
    app = FastAPI(
        title="auth-service",
        version="0.1.0",
        lifespan=lifespan,
        # Interactive API docs only in development.
        docs_url="/auth/docs" if dev else None,
        redoc_url=None,
        openapi_url="/auth/openapi.json" if dev else None,
    )
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
