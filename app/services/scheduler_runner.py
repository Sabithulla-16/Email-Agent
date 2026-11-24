"""Direct in-process and HTTP-triggered scheduler runner.

Allows running scheduled email syncing, Groq analysis, drafting,
and digest generation without requiring a separate paid worker process.
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import get_settings
from app.db.client import get_supabase
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserUpdate
from app.queue.worker import run_user_digest, run_user_sync_pipeline
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.digest_service import DigestService, compute_next_digest_time
from app.services.groq_service import GroqService
from app.services.notification_service import NotificationService
from app.services.sync_service import SyncService

logger = structlog.get_logger("app.services.scheduler_runner")


async def run_direct_sync_cycle(target_user_id: str | None = None) -> dict[str, Any]:
    """Execute one full scheduler cycle directly.

    If target_user_id is provided, syncs only that specific user immediately.
    Otherwise, syncs all active users and dispatches daily digests for due users.
    """
    settings = get_settings()
    client = get_supabase()

    user_repo = UserRepo(client)
    email_repo = EmailRepo(client)
    draft_repo = DraftRepo(client)
    telegram_repo = TelegramRepo(client)

    auth_svc = AuthService(user_repo=user_repo, settings=settings)
    sync_svc = SyncService(
        user_repo=user_repo,
        email_repo=email_repo,
        auth_service=auth_svc,
        settings=settings,
    )
    groq_svc = GroqService(settings=settings)
    approval_svc = ApprovalService(
        draft_repo=draft_repo,
        email_repo=email_repo,
        user_repo=user_repo,
        auth_service=auth_svc,
        settings=settings,
    )
    notif_svc = NotificationService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
    )
    digest_svc = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        auth_service=auth_svc,
        settings=settings,
    )

    ctx = {
        "user_repo": user_repo,
        "email_repo": email_repo,
        "draft_repo": draft_repo,
        "sync_service": sync_svc,
        "groq_service": groq_svc,
        "approval_service": approval_svc,
        "auth_service": auth_svc,
        "notification_service": notif_svc,
        "digest_service": digest_svc,
        "settings": settings,
    }

    if target_user_id:
        target_user = await user_repo.get_by_id(target_user_id)
        if not target_user or target_user.deleted_at:
            return {
                "status": "not_found",
                "claimed_users": 0,
                "new_emails_synced": 0,
                "drafts_created": 0,
                "results": [],
            }
        sync_res = await run_user_sync_pipeline(ctx, str(target_user.id))
        return {
            "status": "success",
            "claimed_users": 1,
            "new_emails_synced": sync_res.get("new_emails_synced", 0),
            "drafts_created": sync_res.get("drafts_created", 0),
            "emails_analyzed": sync_res.get("emails_analyzed", 0),
            "results": [sync_res],
        }

    # Global cycle: sync active users and dispatch due digests
    active_users = await user_repo.list_active_users(limit=50)
    now = datetime.now(UTC)
    results = []
    total_new = 0
    total_drafts = 0

    for user in active_users:
        try:
            # 1. Sync inbox and generate reply drafts
            sync_res = await run_user_sync_pipeline(ctx, str(user.id))
            results.append(sync_res)
            total_new += sync_res.get("new_emails_synced", 0)
            total_drafts += sync_res.get("drafts_created", 0)

            # 2. Check if daily digest is due
            if user.next_digest_at is None:
                next_digest = compute_next_digest_time(
                    digest_time=user.digest_time,
                    user_timezone=user.timezone,
                    now=now,
                )
                await user_repo.update_user(user.id, UserUpdate(next_digest_at=next_digest))
            elif user.next_digest_at <= now:
                await run_user_digest(ctx, str(user.id))
                next_digest = compute_next_digest_time(
                    digest_time=user.digest_time,
                    user_timezone=user.timezone,
                    now=now,
                )
                await user_repo.update_user(user.id, UserUpdate(next_digest_at=next_digest))
        except Exception as exc:
            logger.error(
                "direct_sync_user_failed",
                user_id=str(user.id),
                error=str(exc),
            )

    logger.info("direct_sync_cycle_completed", active_users=len(active_users))
    return {
        "status": "success",
        "claimed_users": len(active_users),
        "new_emails_synced": total_new,
        "drafts_created": total_drafts,
        "results": results,
    }
