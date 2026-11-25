"""Cron trigger API endpoint for serverless or free-tier scheduling."""

import asyncio
import hmac
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, status

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.services.scheduler_runner import run_direct_sync_cycle

logger = structlog.get_logger("app.api.v1.cron")
router = APIRouter(prefix="/cron", tags=["Cron Scheduling"])
_cron_lock = asyncio.Lock()


async def _run_cron_background() -> None:
    """Execute scheduled sync cycle in the background with an in-process overlap guard."""
    if _cron_lock.locked():
        logger.info("cron_cycle_skipped_already_running")
        return
    async with _cron_lock:
        try:
            await run_direct_sync_cycle()
        except Exception as exc:
            logger.error("cron_background_cycle_failed", error=str(exc))


def verify_cron_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    secret: Annotated[str | None, Query(description="Cron secret token")] = None,
    x_cron_secret: Annotated[str | None, Header(description="Cron secret header")] = None,
    authorization: Annotated[str | None, Header(description="Bearer token authorization")] = None,
) -> None:
    """Verify cron caller authorization using constant-time comparison."""
    token = secret or x_cron_secret
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]

    expected_app_secret = settings.APP_SECRET_KEY.get_secret_value()
    expected_tg_secret = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()

    is_valid_app = token and hmac.compare_digest(token, expected_app_secret)
    is_valid_tg = token and hmac.compare_digest(token, expected_tg_secret)

    if not (is_valid_app or is_valid_tg):
        raise ForbiddenError("Invalid or missing cron secret authorization token")


@router.get("/trigger", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_cron_auth)])
@router.post("/trigger", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_cron_auth)])
async def trigger_cron_sync(
    background_tasks: BackgroundTasks,
    wait: Annotated[
        bool, Query(description="Set true to wait synchronously instead of returning immediately")
    ] = False,
) -> dict[str, Any]:
    """Trigger scheduled email sync, Groq analysis, drafting, and digests."""
    if wait:
        result = await run_direct_sync_cycle()
        return {"ok": True, "data": result}

    if _cron_lock.locked():
        return {"ok": True, "data": {"status": "already_running"}}

    background_tasks.add_task(_run_cron_background)
    return {"ok": True, "data": {"status": "accepted"}}
