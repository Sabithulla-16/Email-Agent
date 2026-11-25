"""Structured, PII-safe logging configuration using structlog."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SENSITIVE_EXACT_KEYS = {
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "api_key",
    "client_secret",
    "app_secret_key",
    "token_encryption_key",
    "supabase_secret_key",
}

SENSITIVE_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "api_key",
    "auth_header",
    "bearer",
    "cookie",
    "encryption_key",
    "groq",
    "refresh",
    "body_clean",
)


def pii_masking_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Recursively mask sensitive keys in log event dicts."""
    return _mask_dict(event_dict)


def _mask_dict(data: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(data.items()):
        lower_key = str(key).lower()
        if lower_key in SENSITIVE_EXACT_KEYS or any(s in lower_key for s in SENSITIVE_SUBSTRINGS):
            data[key] = "[REDACTED]"
        elif isinstance(value, MutableMapping):
            data[key] = _mask_dict(value)
        elif isinstance(value, list):
            data[key] = [
                _mask_dict(item) if isinstance(item, MutableMapping) else item for item in value
            ]
    return data


def setup_logging(is_production: bool = False, log_level: str = "INFO") -> None:
    """Configure structlog and standard library logging."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        pii_masking_processor,
    ]

    if is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Suppress verbose loggers
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str = "app") -> structlog.stdlib.BoundLogger:
    """Get a bound structlog logger."""
    return structlog.get_logger(name)
