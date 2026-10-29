"""App factory and lifecycle. Start with: uv run uvicorn auth_service.main:app --reload"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from auth_service.api import health
from auth_service.api.errors import register_error_handlers
from auth_service.api.router import api_router
from auth_service.config import get_settings
from auth_service.core.logging import setup_logging
from auth_service.db.engine import Database
from auth_service.services.cleanup import Retention, cleanup_loop
from auth_service.services.keys import make_signing_keys
from auth_service.services.sms import make_sms_sender

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Runs once per process: code before `yield` at startup, after `yield` at shutdown."""
    settings = get_settings()
    # Signing keys first: a broken JWT_PRIVATE_KEY raises here, so the pod never starts half-ready.
    app.state.keys = make_signing_keys(settings)
    app.state.db = None
    app.state.redis = None
    app.state.sms = make_sms_sender(settings)
    cleanup: asyncio.Task[None] | None = None

    if settings.database_url:
        app.state.db = Database(
            settings.database_url, settings.db_pool_size, settings.db_max_overflow
        )
        cleanup = asyncio.create_task(cleanup_loop(app.state.db, Retention.from_settings(settings)))
    else:
        log.warning("DATABASE_URL not set: database features are disabled")

    if settings.redis_url:
        # Like the DB pool, this connects lazily (on the first command). Short timeouts: a dead
        # Redis must turn into a quick 503, not a hanging request.
        app.state.redis = Redis.from_url(
            settings.redis_url, socket_timeout=1, socket_connect_timeout=1
        )
    else:
        log.error("REDIS_URL not set: /auth/otp/request will answer 503")
    if settings.otp_pepper is None:
        log.error("OTP_PEPPER not set: /auth/otp/request will answer 503")

    log.info("started", extra={"environment": settings.environment})
    yield

    if cleanup is not None:
        cleanup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup
    if app.state.sms is not None:
        await app.state.sms.aclose()  # e.g. the provider's HTTP connection pool
    if app.state.redis is not None:
        await app.state.redis.aclose()
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
    # The client's real IP (request.client.host) from X-Forwarded-For, trusting only the ingress.
    # HAProxy *appends* to a client-sent X-Forwarded-For, so the leftmost entry can be faked;
    # this takes the rightmost address that isn't a trusted proxy = the one HAProxy added.
    # (uvicorn's own --proxy-headers is turned off in the Dockerfile; this is the one place.)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
