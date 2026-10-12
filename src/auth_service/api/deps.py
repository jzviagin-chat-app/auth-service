"""FastAPI dependencies: values that routes receive as parameters (FastAPI's dependency injection).

Example, once a handler needs the database:

    @router.post("/otp/request")
    async def request_otp(body: OtpRequest, db: DbSession) -> OtpResponse: ...
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.db.engine import Database


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request; rolled back automatically if the handler raises."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database not configured")
    async with db.session() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
