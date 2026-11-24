"""Health and readiness probe endpoints."""

from typing import Any

from fastapi import APIRouter, Response, status

from app.db.client import check_supabase_health
from app.queue.redis import check_redis_health

router = APIRouter(tags=["Health"])


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe checking database (Supabase) and queue (Redis) health."""
    db_ok, db_detail = await check_supabase_health()
    redis_ok, redis_detail = await check_redis_health()

    is_ready = db_ok and redis_ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if is_ready else "degraded",
        "services": {
            "database": {"healthy": db_ok, "detail": db_detail},
            "redis": {"healthy": redis_ok, "detail": redis_detail},
        },
    }
