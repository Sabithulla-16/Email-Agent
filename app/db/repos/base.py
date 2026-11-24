"""Base repository providing error handling, retry logic, and pagination."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from postgrest.exceptions import APIError

from app.core.errors import (
    AppError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from supabase import AsyncClient

logger = structlog.get_logger("app.db.repo")

T = TypeVar("T")


class BaseRepo:
    """Base repository wrapping PostgREST operations."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def _execute_with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        max_retries: int = 3,
        base_delay: float = 0.1,
    ) -> T:
        """Execute an async Supabase database operation with bounded retry and jitter."""
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                return await operation()
            except APIError as api_err:
                self._map_api_error(api_err)
            except TimeoutError as timeout_err:
                last_exc = timeout_err
                if attempt == max_retries - 1:
                    raise ExternalServiceError(
                        "Database request timed out",
                        details={"attempts": max_retries},
                    ) from timeout_err
            except Exception as exc:
                last_exc = exc
                if attempt == max_retries - 1:
                    raise

            # Exponential backoff with full jitter
            delay = base_delay * (2**attempt) + random.uniform(0, 0.1)  # noqa: S311
            await asyncio.sleep(delay)

        raise ExternalServiceError(
            "Database operation failed after retries",
            details={"error": str(last_exc)},
        )

    def _map_api_error(self, err: APIError) -> None:
        """Map PostgREST error codes to domain exceptions."""
        code = getattr(err, "code", "")
        message = getattr(err, "message", str(err))
        details = getattr(err, "details", {})

        logger.warning("supabase_postgrest_error", code=code, message=message, details=details)

        # Unique violation (Postgres 23505)
        if (
            code == "23505"
            or "duplicate key" in message.lower()
            or "unique constraint" in message.lower()
        ):
            raise ConflictError(
                "A record with these unique attributes already exists",
                details={"db_error": message},
            )

        # Foreign key violation (Postgres 23503)
        if code == "23503" or "foreign key" in message.lower():
            raise ValidationError(
                "Referenced parent record does not exist",
                details={"db_error": message},
            )

        # Not found (PGRST116: JSON object requested, but 0 rows returned)
        if code == "PGRST116" or "0 rows" in message.lower():
            raise NotFoundError("Requested resource was not found", details={"db_error": message})

        # Check constraint violation (Postgres 23514)
        if code == "23514" or "check constraint" in message.lower():
            raise ValidationError(
                "Data violated validation constraint",
                details={"db_error": message},
            )

        # Generic bad request
        if code.startswith("22") or code.startswith("42"):
            raise ValidationError(f"Invalid database input: {message}", details={"code": code})

        raise AppError(
            f"Database error: {message}",
            code="DATABASE_ERROR",
            status_code=500,
            details={"code": code},
        )
