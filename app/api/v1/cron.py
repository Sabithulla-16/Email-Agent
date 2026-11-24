"""Cron trigger API endpoint for serverless or free-tier scheduling."""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.services.scheduler_runner import run_direct_sync_cycle

router = APIRouter(prefix="/cron", tags=["Cron Scheduling"])


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
async def trigger_cron_sync() -> dict[str, Any]:
    """Execute scheduled email sync, Groq analysis, drafting, and digests for due users."""
    result = await run_direct_sync_cycle()
    return {"ok": True, "data": result}
