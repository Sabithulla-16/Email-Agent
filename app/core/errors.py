"""Standard error types and domain exception hierarchy."""

from typing import Any


class AppError(Exception):
    """Base application domain exception."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_SERVER_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class NotFoundError(AppError):
    def __init__(
        self,
        message: str = "Resource not found",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code="NOT_FOUND", status_code=404, details=details)


class ConflictError(AppError):
    def __init__(
        self,
        message: str = "Resource state conflict",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code="CONFLICT", status_code=409, details=details)


class UnauthorizedError(AppError):
    def __init__(
        self,
        message: str = "Authentication required",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code="UNAUTHORIZED", status_code=401, details=details)


class ForbiddenError(AppError):
    def __init__(
        self,
        message: str = "Permission denied",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code="FORBIDDEN", status_code=403, details=details)


class ValidationError(AppError):
    def __init__(
        self,
        message: str = "Validation failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code="VALIDATION_ERROR", status_code=422, details=details)


class RateLimitExceededError(AppError):
    def __init__(
        self,
        message: str = "Rate limit exceeded",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="RATE_LIMIT_EXCEEDED",
            status_code=429,
            details=details,
        )


class ExternalServiceError(AppError):
    def __init__(
        self,
        message: str = "External service unavailable",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="EXTERNAL_SERVICE_ERROR",
            status_code=502,
            details=details,
        )
