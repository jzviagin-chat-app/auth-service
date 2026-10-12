"""Async connection pool to Postgres (SQLAlchemy 2.0 + asyncpg driver).

Like a `pg.Pool` in Node: created once at startup, shared by all requests, closed at shutdown.
Creating it does NOT connect; the first query does.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def to_async_url(url: str) -> str:
    """The Secret holds a plain `postgresql://` URL; SQLAlchemy needs to be told to use asyncpg."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url


class Database:
    def __init__(self, url: str, pool_size: int, max_overflow: int) -> None:
        self.engine: AsyncEngine = create_async_engine(
            to_async_url(url),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # drop dead connections (e.g. after a Postgres restart)
        )
        # expire_on_commit=False: objects stay readable after commit (no surprise lazy reloads,
        # which would need an `await` in async code).
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def dispose(self) -> None:
        await self.engine.dispose()
