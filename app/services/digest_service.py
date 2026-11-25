"""Daily digest generation and dispatch service."""

import contextlib
import inspect
import zoneinfo
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import DraftStatus, UserUpdate
from app.services.followup_service import FollowupService
from app.services.groq_service import GroqService
from app.services.telegram_client import TelegramClient, escape_html
from app.services.voice_service import (
    VoiceService,
    build_spoken_script,
    is_voice_enabled,
    resolve_voice_name,
)

logger = structlog.get_logger("app.services.digest")

# Default 3x daily digest slots in user's local timezone (9 AM, 3 PM, 9 PM)
DEFAULT_DIGEST_SLOTS: tuple[time, ...] = (time(9, 0), time(15, 0), time(21, 0))


def _resolve_tz(user_timezone: str) -> Any:
    try:
        return zoneinfo.ZoneInfo(user_timezone)
    except Exception:
        return UTC


def _normalize_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def compute_next_digest_time(
    digest_time: time,
    user_timezone: str = "Asia/Kolkata",
    now: datetime | None = None,
    digest_slots: Sequence[time] | None = None,
) -> datetime:
    """Calculate next wall-clock occurrence of digest slot(s) in user_timezone as UTC."""
    tz = _resolve_tz(user_timezone)
    now_dt = now or datetime.now(tz)
    now_dt = now_dt.replace(tzinfo=tz) if now_dt.tzinfo is None else now_dt.astimezone(tz)

    slots = sorted(digest_slots) if digest_slots else [digest_time]
    for slot in slots:
        candidate = datetime.combine(now_dt.date(), slot, tzinfo=tz)
        if candidate > now_dt:
            return candidate.astimezone(UTC)

    # All today's slots have passed; pick the earliest slot tomorrow
    tomorrow = now_dt.date() + timedelta(days=1)
    return datetime.combine(tomorrow, slots[0], tzinfo=tz).astimezone(UTC)


def get_digest_window_and_label(
    user_timezone: str = "Asia/Kolkata",
    now: datetime | None = None,
    last_digest_at: datetime | None = None,
) -> tuple[datetime, str, bool]:
    """Determine (window_start_utc, report_title, is_incremental_slot) for 9 AM / 3 PM / 9 PM."""
    tz = _resolve_tz(user_timezone)
    now_utc = _normalize_utc(now)
    local_now = now_utc.astimezone(tz)
    tz_abbr = local_now.tzname() or user_timezone

    hour = local_now.hour
    if hour < 12:
        # 9:00 AM Morning report: covers previous 9:00 PM -> 9:00 AM
        default_since_local = datetime.combine(
            local_now.date() - timedelta(days=1), time(21, 0), tzinfo=tz
        )
        title = f"◈ <b>Morning Digest (9:00 PM – 9:00 AM {escape_html(tz_abbr)})</b>"
        is_incremental = False
    elif hour < 18:
        # 3:00 PM Afternoon report: covers extra emails after 9:00 AM
        default_since_local = datetime.combine(local_now.date(), time(9, 0), tzinfo=tz)
        title = f"◈ <b>Afternoon Update (9:00 AM – 3:00 PM {escape_html(tz_abbr)})</b>"
        is_incremental = True
    else:
        # 9:00 PM Evening report: covers extra emails after 3:00 PM
        default_since_local = datetime.combine(local_now.date(), time(15, 0), tzinfo=tz)
        title = f"◈ <b>Evening Update (3:00 PM – 9:00 PM {escape_html(tz_abbr)})</b>"
        is_incremental = True

    default_since_utc = default_since_local.astimezone(UTC)
    if last_digest_at is not None:
        last_utc = (
            last_digest_at.astimezone(UTC)
            if last_digest_at.tzinfo
            else last_digest_at.replace(tzinfo=UTC)
        )
        # Use last_digest_at if it's recent (within 24h) and before now
        if now_utc - timedelta(hours=24) <= last_utc < now_utc:
            return last_utc, title, is_incremental

    return default_since_utc, title, is_incremental


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
        auth_service: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.user_repo = user_repo
        self.email_repo = email_repo
        self.draft_repo = draft_repo
        self.telegram_repo = telegram_repo
        self.auth_service = auth_service
        self.settings = settings or get_settings()
        self.telegram_client = telegram_client or TelegramClient(
            telegram_repo=telegram_repo, settings=self.settings
        )
        self.groq_service = groq_service or GroqService(settings=self.settings)

    async def generate_and_dispatch_digest(
        self,
        user_id: UUID | str,
        force: bool = False,
        now: datetime | None = None,
        include_voice: bool | None = None,
    ) -> dict[str, Any]:
        """Compile email activity for the current digest window (9 AM / 3 PM / 9 PM IST)."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return {"status": "skipped", "reason": "user_not_found"}

        prefs = await self.user_repo.get_notification_prefs(user.id)
        if prefs and not prefs.digest_enabled:
            logger.info("digest_skipped_user_disabled", user_id=str(user.id))
            return {"status": "skipped", "reason": "digest_disabled_in_prefs"}

        now_utc = _normalize_utc(now)
        user_tz = user.timezone if user.timezone and user.timezone != "UTC" else "Asia/Kolkata"
        window_start, report_title, is_incremental = get_digest_window_and_label(
            user_timezone=user_tz,
            now=now_utc,
            last_digest_at=getattr(user, "last_digest_at", None),
        )

        # 1. Fetch recent emails in this window and pending drafts
        user_groq_key: str | None = None
        if (
            hasattr(self, "auth_service")
            and self.auth_service
            and hasattr(self.auth_service, "get_user_groq_api_key")
        ):
            try:
                res_k = self.auth_service.get_user_groq_api_key(user_id)
                if hasattr(res_k, "__await__"):
                    user_groq_key = await res_k
                else:
                    user_groq_key = res_k
            except Exception:
                user_groq_key = None

        list_since_fn = getattr(self.email_repo, "list_by_user_since", None)
        if inspect.iscoroutinefunction(list_since_fn) and not force:
            emails = await list_since_fn(
                user_id=user.id, since=window_start, until=now_utc, limit=30
            )
        else:
            emails = await self.email_repo.list_by_user(user_id=user.id, limit=20)

        # For 3 PM and 9 PM incremental slots, skip notification if no extra emails arrived
        update_user_fn = getattr(self.user_repo, "update_user", None)
        if is_incremental and not emails and not force:
            if inspect.iscoroutinefunction(update_user_fn):
                with contextlib.suppress(Exception):
                    await update_user_fn(user.id, UserUpdate(last_digest_at=now_utc))
            logger.info("incremental_digest_skipped_no_new_emails", user_id=str(user.id))
            return {"status": "skipped", "reason": "no_new_emails"}

        pending_drafts = await self.draft_repo.list_by_user(
            user_id=user.id, status=DraftStatus.PENDING_APPROVAL.value, limit=50
        )

        total_emails = len(emails)
        total_pending = len(pending_drafts)

        # 2. Generate short summaries for emails in window with Qwen/Llama fallback
        email_summaries: list[dict[str, str]] = []
        urgent_emails: list[str] = []

        for em in emails[:15]:
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
                        api_key=user_groq_key,
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
            f"{report_title}",
            f"<i>For {escape_html(user.name or 'there')}</i>\n",
            f"• <b>Emails in Window:</b> {total_emails}",
            f"• <b>Drafts Ready for Approval:</b> {total_pending}\n",
        ]

        if urgent_emails:
            digest_lines.append("▸ <b>High Priority Items:</b>")
            for subj in urgent_emails[:3]:
                digest_lines.append(f"• {escape_html(subj)}")
            digest_lines.append("")

        if email_summaries:
            digest_lines.append("▸ <b>Emails Summary:</b>")
            for item in email_summaries:
                sender = escape_html(item["sender"])
                subject = escape_html(item["subject"])
                summ = escape_html(item["summary"])
                digest_lines.append(f"• <b>{sender}:</b> {subject}\n  └ <i>{summ}</i>")
            digest_lines.append("")

        if total_pending > 0:
            digest_lines.append(
                f"› <i>You have {total_pending} draft(s) awaiting review. "
                "Use /pending to review and approve.</i>"
            )

        # Include Decision & Commitment Radar on Evening Update (9 PM)
        local_hour = now_utc.astimezone(_resolve_tz(user_tz)).hour
        if local_hour >= 18 and not force:
            try:
                radar = await FollowupService().get_radar_summary(
                    user_id=user.id, email_repo=self.email_repo, limit=3
                )
                w_you = radar.get("waiting_on_you", [])
                w_others = radar.get("waiting_on_others", [])
                if w_you or w_others:
                    digest_lines.append("\n▸ <b>Decision &amp; Commitment Radar:</b>")
                    for item in w_you[:2]:
                        digest_lines.append(
                            f"• Waiting on you: <b>{escape_html(item['sender'])}</b> — "
                            f"{escape_html(item['subject'])}"
                        )
                    for item in w_others[:2]:
                        digest_lines.append(
                            f"• Open commitment: {escape_html(item['label'])} "
                            f"(Due: {escape_html(item['due_date'])})"
                        )
            except Exception as exc:
                logger.debug("digest_radar_skipped", error=str(exc))

        full_digest_text = "\n".join(digest_lines)

        # Record last_digest_at so subsequent 3 PM / 9 PM reports only include new emails
        if inspect.iscoroutinefunction(update_user_fn):
            try:
                await update_user_fn(user.id, UserUpdate(last_digest_at=now_utc))
            except Exception as exc:
                logger.warning("digest_update_last_digest_failed", error=str(exc))

        # 4. Dispatch to Telegram if linked (always send text; also send voice on morning/forced)
        telegram_link = await self.telegram_repo.get_link_by_user_id(user.id)
        if telegram_link and telegram_link.is_active:
            keyboard = None
            frontend_url = self.settings.FRONTEND_BASE_URL.rstrip("/")
            if frontend_url and not ("localhost" in frontend_url or "127.0.0.1" in frontend_url):
                keyboard = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "▸ Review Pending Drafts",
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

            should_send_voice = (
                include_voice
                if include_voice is not None
                else (not is_incremental and is_voice_enabled(user.prefs))
            )
            voice_sent = False
            send_voice_fn = getattr(self.telegram_client, "send_voice", None)
            if should_send_voice and inspect.iscoroutinefunction(send_voice_fn):
                try:
                    script = build_spoken_script(
                        user_name=user.name or "there",
                        total_emails=total_emails,
                        total_pending=total_pending,
                        urgent_emails=urgent_emails,
                        email_summaries=email_summaries,
                    )
                    voice_name = resolve_voice_name(user.prefs)
                    audio_bytes = await VoiceService().synthesize_speech(
                        text=script, voice_name=voice_name
                    )
                    if audio_bytes:
                        await send_voice_fn(
                            chat_id=telegram_link.chat_id,
                            audio_bytes=audio_bytes,
                            caption="◉ <i>Morning Audio Briefing</i>",
                        )
                        voice_sent = True
                except Exception as exc:
                    logger.warning("digest_voice_briefing_failed", error=str(exc))

            return {
                "status": "delivered_telegram",
                "chat_id": telegram_link.chat_id,
                "emails_summarized": total_emails,
                "pending_drafts": total_pending,
                "voice_sent": voice_sent,
            }

        logger.info("digest_compiled_web", user_id=str(user.id), emails=total_emails)
        return {
            "status": "compiled_web",
            "emails_summarized": total_emails,
            "pending_drafts": total_pending,
        }
