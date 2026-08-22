"""Domain exceptions mapped to RFC 9457 (application/problem+json)
responses. Every endpoint in this API returns errors in this one shape —
FastAPI's default validation/HTTPException bodies are overridden app-wide.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code: int = 500
    title: str = "Internal Server Error"
    type_uri: str = "about:blank"

    def __init__(self, detail: str, **extra: Any) -> None:
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


class NotFoundError(AppError):
    status_code = 404
    title = "Not Found"


class ConflictError(AppError):
    status_code = 409
    title = "Conflict"


class ValidationError(AppError):
    status_code = 422
    title = "Validation Error"


class Unauthorized(AppError):
    status_code = 401
    title = "Unauthorized"


class Forbidden(AppError):
    status_code = 403
    title = "Forbidden"


class RateLimitedError(AppError):
    status_code = 429
    title = "Too Many Requests"


class IdempotencyConflict(AppError):
    status_code = 409
    title = "Idempotency Key Conflict"


def _problem(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
    type_uri: str = "about:blank",
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": getattr(request.state, "request_id", None),
        **extra,
    }
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        extra = dict(exc.extra)
        headers: dict[str, str] = {}
        retry_after = extra.pop("retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return _problem(
            request, exc.status_code, exc.title, exc.detail, exc.type_uri, headers, **extra
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            request,
            422,
            "Validation Error",
            "Request validation failed",
            errors=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return _problem(request, exc.status_code, "HTTP Error", detail)

    @app.exception_handler(Exception)
    async def handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), path=request.url.path)
        return _problem(request, 500, "Internal Server Error", "An unexpected error occurred")
