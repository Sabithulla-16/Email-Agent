"""Telegram webhook endpoint and account linking API routes."""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status
from pydantic import BaseModel

from app.api.deps import (
    get_auth_service,
    get_current_user,
    get_draft_repo,
    get_email_repo,
    get_telegram_repo,
    get_user_repo,
    verify_csrf,
)
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserRow
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.notification_service import NotificationService
from app.services.telegram_service import TelegramService

router = APIRouter(prefix="/telegram", tags=["Telegram Bot & Notifications"])


def get_telegram_service(
    telegram_repo: Annotated[TelegramRepo, Depends(get_telegram_repo)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TelegramService:
    approval_svc = ApprovalService(
        draft_repo=draft_repo,
        email_repo=email_repo,
        user_repo=user_repo,
        auth_service=auth_service,
    )
    notification_svc = NotificationService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
    )
    return TelegramService(
        telegram_repo=telegram_repo,
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        approval_service=approval_svc,
        notification_service=notification_svc,
    )


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------
class TelegramLinkResponse(BaseModel):
    deep_link: str
    expires_in_seconds: int = 600


class TelegramStatusResponse(BaseModel):
    is_linked: bool
    chat_id: int | None = None


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@router.post(
    "/link",
    response_model=TelegramLinkResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_csrf)],
)
async def generate_telegram_link(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    telegram_service: Annotated[TelegramService, Depends(get_telegram_service)],
) -> TelegramLinkResponse:
    """Generate deep link to start Telegram bot account linking."""
    deep_link = await telegram_service.create_link_deep_link(current_user.id)
    return TelegramLinkResponse(deep_link=deep_link)


@router.get("/status", response_model=TelegramStatusResponse)
async def get_telegram_status(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    telegram_repo: Annotated[TelegramRepo, Depends(get_telegram_repo)],
) -> TelegramStatusResponse:
    """Check if current user has an active Telegram account linked."""
    link = await telegram_repo.get_link_by_user_id(current_user.id)
    if not link or not link.is_active:
        return TelegramStatusResponse(is_linked=False)
    return TelegramStatusResponse(is_linked=True, chat_id=link.chat_id)


@router.delete("/link", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_csrf)])
async def unlink_telegram(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    telegram_repo: Annotated[TelegramRepo, Depends(get_telegram_repo)],
) -> dict[str, Any]:
    """Deactivate Telegram account link for the current user."""
    link = await telegram_repo.get_link_by_user_id(current_user.id)
    if link:
        await telegram_repo.deactivate_chat(link.chat_id)
    return {"status": "unlinked"}


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    telegram_service: Annotated[TelegramService, Depends(get_telegram_service)],
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """Public webhook receiver for Telegram Bot API updates."""
    # Constant-time header verification
    telegram_service.verify_webhook_secret(x_telegram_bot_api_secret_token)

    update_payload = await request.json()
    background_tasks.add_task(telegram_service.process_webhook_update, update_payload)
    return {"ok": True}
