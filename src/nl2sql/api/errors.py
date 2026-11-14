"""Typed error envelope. Exception text is never returned to clients."""

from __future__ import annotations

import contextvars
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def envelope(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Any:
    rid = request_id_ctx.get()
    body = {
        "error": {"code": code, "message": message, "request_id": rid},
        # `detail` keeps the bundled frontend (which reads data.detail) working.
        "detail": message,
    }
    return JSONResponse(body, status_code=status, headers=headers)


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    405: "method_not_allowed",
    413: "too_large",
    429: "rate_limited",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> Any:
        return envelope(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> Any:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return envelope(exc.status_code, code, "Request could not be processed.")

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> Any:
        return envelope(422, "invalid_request", "Request body failed validation.")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> Any:
        log.error("unhandled_error", error_type=type(exc).__name__, exc_info=exc)
        return envelope(500, "internal_error", "Internal server error.")
