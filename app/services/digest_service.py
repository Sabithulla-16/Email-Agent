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
from app.services.groq_service import GroqService
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
        groq_service: GroqService | None = None,
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
        self.groq_service = groq_service or GroqService(settings=self.settings)

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
        emails = await self.email_repo.list_by_user(user_id=user.id, limit=20)
        pending_drafts = await self.draft_repo.list_by_user(
            user_id=user.id, status=DraftStatus.PENDING_APPROVAL.value, limit=50
        )

        total_emails = len(emails)
        total_pending = len(pending_drafts)

        # 2. Generate short summaries for recent emails with Qwen/Llama fallback
        email_summaries: list[dict[str, str]] = []
        urgent_emails: list[str] = []

        for em in emails[:6]:
            if "urgent" in em.subject.lower():
                urgent_emails.append(em.subject)

            summary_text = ""
            # Check if email already has a saved analysis summary
            try:
                analysis = await self.email_repo.get_analysis(em.id)
                if analysis and analysis.summary:
                    summary_text = analysis.summary
            except Exception:
                pass

            # If no analysis summary, use LLM with automatic model fallback
            if not summary_text:
                try:
                    snippet_content = em.snippet or em.subject
                    prompt = (
                        "Summarize this email in ONE very short sentence (max 15 words).\n"
                        f"From: {em.sender}\n"
                        f"Subject: {em.subject}\n"
                        f"Content: {snippet_content[:600]}\n"
                    )
                    summary_text, _ = await self.groq_service.chat_completion_with_fallback(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.2,
                        max_tokens=50,
                    )
                except Exception as exc:
                    logger.warning(
                        "digest_email_summary_failed", email_id=str(em.id), error=str(exc)
                    )
                    # Safe fallback snippet without server crashing
                    summary_text = (em.snippet or em.subject)[:100]

            email_summaries.append(
                {
                    "sender": em.sender,
                    "subject": em.subject,
                    "summary": summary_text,
                }
            )

        # 3. Format clean HTML digest
        digest_lines = [
            f"☀️ <b>Daily Executive Digest for {escape_html(user.name or 'there')}</b>\n",
            f"• <b>Total Received:</b> {total_emails}",
            f"• <b>Drafts Ready for Approval:</b> {total_pending}\n",
        ]

        if urgent_emails:
            digest_lines.append("🚨 <b>High Priority Items:</b>")
            for subj in urgent_emails[:3]:
                digest_lines.append(f"• {escape_html(subj)}")
            digest_lines.append("")

        if email_summaries:
            digest_lines.append("📬 <b>Recent Emails Summary:</b>")
            for item in email_summaries:
                sender = escape_html(item["sender"])
                subject = escape_html(item["subject"])
                summ = escape_html(item["summary"])
                digest_lines.append(f"• <b>{sender}:</b> {subject}\n  └ <i>{summ}</i>")
            digest_lines.append("")

        if total_pending > 0:
            digest_lines.append(
                f"👉 <i>You have {total_pending} draft(s) awaiting review. "
                "Type /pending to review and approve.</i>"
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
