"""Core FastAPI middlewares: Request ID, Security Headers, and Exception Formatting."""

import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings
from app.core.errors import AppError

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def get_current_request_id() -> str:
    """Retrieve current request ID from contextvar."""
    return request_id_ctx.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns or propagates X-Request-ID on every request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_ctx.set(req_id)
        structlog.contextvars.bind_contextvars(request_id=req_id)

        start_time = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            response.headers["X-Response-Time"] = f"{duration_ms}ms"
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_ctx.reset(token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Enforces standard HTTP security response headers."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains; preload"
        )
        return response


def setup_middleware(app: FastAPI, settings: Settings) -> None:
    """Register all middlewares on the application."""
    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)
    # Request ID
    app.add_middleware(RequestIDMiddleware)


def register_exception_handlers(app: FastAPI) -> None:
    """Register uniform error handlers returning {code, message, request_id}."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        req_id = get_current_request_id() or request.headers.get("X-Request-ID", "unknown")
        content: dict[str, object] = {
            "code": exc.code,
            "message": exc.message,
            "request_id": req_id,
        }
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = get_current_request_id() or request.headers.get("X-Request-ID", "unknown")
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "request_id": req_id,
                "details": {"errors": exc.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        req_id = get_current_request_id() or request.headers.get("X-Request-ID", "unknown")
        logger = structlog.get_logger("app.unhandled_exception")
        logger.exception("unhandled_server_error", exc_info=exc, path=str(request.url))
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred",
                "request_id": req_id,
            },
        )
