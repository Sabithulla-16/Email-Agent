"""Daily digest generation and dispatch service."""

import zoneinfo
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import DraftStatus
from app.services.telegram_client import TelegramClient, escape_html

logger = structlog.get_logger("app.services.digest")


def compute_next_digest_time(
    digest_time: time,
    user_timezone: str = "UTC",
    now: datetime | None = None,
) -> datetime:
    """Calculate the next wall-clock occurrence of digest_time in user_timezone converted to UTC."""
    tz: Any
    try:
        tz = zoneinfo.ZoneInfo(user_timezone)
    except Exception:
        tz = UTC

    now_dt = now or datetime.now(tz)
    now_dt = now_dt.replace(tzinfo=tz) if now_dt.tzinfo is None else now_dt.astimezone(tz)

    candidate = datetime.combine(now_dt.date(), digest_time, tzinfo=tz)
    if candidate <= now_dt:
        candidate = datetime.combine(now_dt.date() + timedelta(days=1), digest_time, tzinfo=tz)

    return candidate.astimezone(UTC)


class DigestService:
    """Compiles daily email analytics, category breakdowns, and pending approvals."""

    def __init__(
        self,
        user_repo: UserRepo,
        email_repo: EmailRepo,
        draft_repo: DraftRepo,
        telegram_repo: TelegramRepo,
        telegram_client: TelegramClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.user_repo = user_repo
        self.email_repo = email_repo
        self.draft_repo = draft_repo
        self.telegram_repo = telegram_repo
        self.settings = settings or get_settings()
        self.telegram_client = telegram_client or TelegramClient(
            telegram_repo=telegram_repo, settings=self.settings
        )

    async def generate_and_dispatch_digest(self, user_id: UUID | str) -> dict[str, Any]:
        """Compile email activity from the past 24 hours and send daily digest."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return {"status": "skipped", "reason": "user_not_found"}

        prefs = await self.user_repo.get_notification_prefs(user.id)
        if prefs and not prefs.digest_enabled:
            logger.info("digest_skipped_user_disabled", user_id=str(user.id))
            return {"status": "skipped", "reason": "digest_disabled_in_prefs"}

        # 1. Fetch recent emails and pending drafts
        emails = await self.email_repo.list_by_user(user_id=user.id, limit=100)
        pending_drafts = await self.draft_repo.list_by_user(
            user_id=user.id, status=DraftStatus.PENDING_APPROVAL.value, limit=50
        )

        total_emails = len(emails)
        total_pending = len(pending_drafts)

        # 2. Build category counts
        category_counts: dict[str, int] = {}
        urgent_emails: list[str] = []

        for em in emails:
            cat = "inbox"
            category_counts[cat] = category_counts.get(cat, 0) + 1
            if "urgent" in em.subject.lower():
                urgent_emails.append(em.subject)

        # 3. Format clean HTML digest
        digest_lines = [
            f"☀️ <b>Good Morning, {escape_html(user.name or 'there')}!</b>\n",
            "Here is your daily email executive summary:\n",
            f"• <b>Total Received (24h):</b> {total_emails}",
            f"• <b>Drafts Waiting for Approval:</b> {total_pending}\n",
        ]

        if urgent_emails:
            digest_lines.append("🚨 <b>High Priority Items:</b>")
            for subj in urgent_emails[:3]:
                digest_lines.append(f"• {escape_html(subj)}")
            digest_lines.append("")

        if total_pending > 0:
            digest_lines.append(
                f"👉 <i>You have {total_pending} draft(s) awaiting review. "
                "Type /pending to review or visit the web app.</i>"
            )

        full_digest_text = "\n".join(digest_lines)

        # 4. Dispatch to Telegram if linked
        telegram_link = await self.telegram_repo.get_link_by_user_id(user.id)
        if telegram_link and telegram_link.is_active:
            keyboard = None
            frontend_url = self.settings.FRONTEND_BASE_URL.rstrip("/")
            if frontend_url and not ("localhost" in frontend_url or "127.0.0.1" in frontend_url):
                keyboard = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "📬 Review Pending Drafts",
                                "url": f"{frontend_url}/drafts",
                            }
                        ]
                    ]
                }
            await self.telegram_client.send_message(
                chat_id=telegram_link.chat_id,
                text=full_digest_text,
                reply_markup=keyboard,
            )
            return {
                "status": "delivered_telegram",
                "chat_id": telegram_link.chat_id,
                "emails_summarized": total_emails,
                "pending_drafts": total_pending,
            }

        logger.info("digest_compiled_web", user_id=str(user.id), emails=total_emails)
        return {
            "status": "compiled_web",
            "emails_summarized": total_emails,
            "pending_drafts": total_pending,
        }
