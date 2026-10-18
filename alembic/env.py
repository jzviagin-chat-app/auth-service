"""Alembic migration runner (async). The database URL comes from DATABASE_URL, like the app.

After changing db/models.py, create a migration:
    uv run alembic revision --autogenerate -m "add users" --rev-id 0002
Apply migrations:
    uv run alembic upgrade head

In the cluster this runs as the `migrate` init container of every auth pod, before the app starts.
"""

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from auth_service.config import get_settings
from auth_service.db.engine import to_async_url
from auth_service.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
log = logging.getLogger("alembic.env")

target_metadata = Base.metadata  # autogenerate compares the models with the real database

# Several pods can start at once (2 replicas, rolling deploys): only one may migrate at a time.
# Any fixed number works; it just has to be the same for every pod.
MIGRATION_LOCK_ID = 7_245_001
CONNECT_ATTEMPTS = 30  # x 2 s: new pods are blocked by the NetworkPolicy for a few seconds


def database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return to_async_url(url)


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Session-level lock: waits while another pod migrates, released when we disconnect.
    connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    connection.commit()  # Alembic must start its own transaction, not join ours
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def connect_with_retry(engine: AsyncEngine) -> AsyncConnection:
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return await engine.connect()
        except (OSError, DBAPIError) as e:
            if attempt == CONNECT_ATTEMPTS:
                raise
            log.warning("database not reachable (attempt %d): %s", attempt, type(e).__name__)
            await asyncio.sleep(2)
    raise AssertionError("unreachable")


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        connection = await connect_with_retry(engine)
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            await connection.close()  # also releases the advisory lock
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
