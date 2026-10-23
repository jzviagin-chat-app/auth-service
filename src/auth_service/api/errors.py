"""One error shape for every response: {"error": "<machine_code>", "message": "<human text>"}.

Clients branch on `error`, never on `message` (README, "API"). FastAPI's own errors look different
({"detail": ...}), so the handlers below convert them. Like an Express error-handling middleware.
"""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Machine codes for errors FastAPI/Starlette raise themselves.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
    503: "service_unavailable",
}


class ApiError(Exception):
    """Raise from a route to answer with our error shape: ApiError(400, "invalid_phone", "...")."""

    def __init__(
        self, status_code: int, error: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.headers = headers


def error_response(
    status_code: int, error: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code, headers=headers)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # noqa: S101 (registered for ApiError only)
    return error_response(exc.status_code, exc.error, exc.message, exc.headers)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    error = _STATUS_CODES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, error, str(exc.detail), exc.headers)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    """Malformed body/query. Names the fields only, never their values (may be phone numbers)."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    fields = sorted({".".join(str(p) for p in e["loc"] if p != "body") for e in exc.errors()})
    return error_response(422, "invalid_request", "Invalid or missing fields: " + ", ".join(fields))


async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """A bug or an outage (e.g. the database is down). uvicorn still logs the traceback."""
    return error_response(500, "internal_error", "Something went wrong; try again later")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
