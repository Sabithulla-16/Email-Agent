"""Supabase async client lifecycle and connection management."""

import structlog
from postgrest.types import CountMethod

from app.core.config import Settings, get_settings
from supabase import AsyncClient, create_async_client

logger = structlog.get_logger("app.db.client")

_supabase_client: AsyncClient | None = None


async def init_supabase(settings: Settings | None = None) -> AsyncClient:
    """Initialize the singleton async Supabase client."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    cfg = settings or get_settings()
    url = cfg.SUPABASE_URL
    # Server-side ONLY secret key (never anon key)
    key = cfg.SUPABASE_SECRET_KEY.get_secret_value()

    logger.info("initializing_supabase_client", url=url)
    _supabase_client = await create_async_client(url, key)
    return _supabase_client


def get_supabase() -> AsyncClient:
    """Get the active async Supabase client instance."""
    if _supabase_client is None:
        raise RuntimeError("Supabase client has not been initialized. Call init_supabase() first.")
    return _supabase_client


async def close_supabase() -> None:
    """Close the Supabase client if open."""
    global _supabase_client
    _supabase_client = None
    logger.info("supabase_client_closed")


async def check_supabase_health() -> tuple[bool, str]:
    """Check connectivity to Supabase.

    Returns (healthy: bool, detail: str).
    """
    if _supabase_client is None:
        return False, "Supabase client not initialized"
    try:
        # Quick non-blocking query checking schema access
        await (
            _supabase_client.table("users").select("id", count=CountMethod.exact).limit(0).execute()
        )
        return True, "connected"
    except Exception as exc:
        logger.warning("supabase_health_check_failed", error=str(exc))
        return False, str(exc)
