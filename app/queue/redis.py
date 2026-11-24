"""Redis connection pool and health checks."""

import redis.asyncio as aioredis
import structlog

from app.core.config import Settings, get_settings

logger = structlog.get_logger("app.queue.redis")

_redis_pool: aioredis.Redis | None = None


async def init_redis(settings: Settings | None = None) -> aioredis.Redis:
    """Initialize Redis client pool."""
    global _redis_pool
    if _redis_pool is not None:
        return _redis_pool

    cfg = settings or get_settings()
    logger.info("initializing_redis_pool", url=cfg.REDIS_URL.split("@")[-1])
    _redis_pool = aioredis.from_url(  # type: ignore[no-untyped-call]
        cfg.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    return _redis_pool


def get_redis() -> aioredis.Redis | None:
    """Retrieve the Redis client instance or None if not initialized."""
    return _redis_pool


async def close_redis() -> None:
    """Close Redis client pool."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None
        logger.info("redis_pool_closed")


async def check_redis_health() -> tuple[bool, str]:
    """Check connectivity to Redis server."""
    if _redis_pool is None:
        return False, "Redis pool not initialized"
    try:
        pong = await _redis_pool.ping()
        if pong:
            return True, "connected"
        return False, "unexpected ping response"
    except Exception as exc:
        logger.warning("redis_health_check_failed", error=str(exc))
        return False, str(exc)
