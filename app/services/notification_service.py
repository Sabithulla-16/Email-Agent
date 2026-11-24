"""Notification delivery service supporting Telegram, quiet hours, snooze, and minimal mode."""

import secrets
import zoneinfo
from datetime import UTC, datetime, time, timedelta
from typing import Any

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    DraftRow,
    DraftVersionRow,
    EmailAnalysisRow,
    EmailPriority,
    EmailRow,
    NotificationPrefsRow,
    UserRow,
)
from app.services.extra_extractors import ExtraExtractorsService
from app.services.smart_actions import SmartActionsService
from app.services.telegram_client import TelegramClient, escape_html

logger = structlog.get_logger("app.services.notification")


class NotificationService:
    """Manages notification dispatch across channels with quiet-hours and snooze enforcement."""

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

    def is_in_quiet_hours(
        self,
        quiet_start: time | None,
        quiet_end: time | None,
        user_timezone: str = "UTC",
    ) -> bool:
        """Evaluate if current wall-clock time in user timezone falls into quiet hours."""
        if not quiet_start or not quiet_end:
            return False

        tz: Any
        try:
            tz = zoneinfo.ZoneInfo(user_timezone)
        except Exception:
            tz = UTC

        now_local = datetime.now(tz).time()

        if quiet_start <= quiet_end:
            return quiet_start <= now_local <= quiet_end
        else:
            # Over midnight (e.g. 22:00 to 07:00)
            return now_local >= quiet_start or now_local <= quiet_end

    async def should_suppress_notification(
        self,
        user: UserRow,
        prefs: NotificationPrefsRow | None,
        priority: EmailPriority = EmailPriority.MEDIUM,
    ) -> bool:
        """Check snooze timestamp and quiet hours."""
        now = datetime.now(UTC)

        if prefs:
            # 1. Snooze check
            if prefs.snoozed_until and prefs.snoozed_until > now:
                logger.info("notification_suppressed_by_snooze", user_id=str(user.id))
                return True

            # 2. Quiet hours check (high priority bypasses quiet hours)
            if (
                priority != EmailPriority.HIGH
                and prefs.quiet_start
                and prefs.quiet_end
                and self.is_in_quiet_hours(prefs.quiet_start, prefs.quiet_end, user.timezone)
            ):
                logger.info("notification_suppressed_by_quiet_hours", user_id=str(user.id))
                return True

        return False

    async def notify_new_draft(
        self,
        draft: DraftRow,
        version: DraftVersionRow,
        email: EmailRow,
        analysis: EmailAnalysisRow | None = None,
    ) -> dict[str, Any]:
        """Format and dispatch new draft notification to user's configured channel."""
        user = await self.user_repo.get_by_id(draft.user_id)
        if not user:
            return {"status": "skipped", "reason": "user_not_found"}

        prefs = await self.user_repo.get_notification_prefs(user.id)
        priority = analysis.priority if analysis else EmailPriority.MEDIUM

        if await self.should_suppress_notification(user, prefs, priority):
            return {"status": "suppressed", "reason": "quiet_or_snoozed"}

        # Channel selection: default to Telegram if linked, otherwise web
        telegram_link = await self.telegram_repo.get_link_by_user_id(user.id)
        channel = prefs.channel if prefs else "telegram"

        if channel == "none":
            return {"status": "skipped", "reason": "notifications_disabled"}

        if channel == "telegram" and telegram_link and telegram_link.is_active:
            return await self._dispatch_telegram_draft_notification(
                chat_id=telegram_link.chat_id,
                user=user,
                draft=draft,
                version=version,
                email=email,
                analysis=analysis,
            )

        # Web / push notification fallback (stored in database / logged)
        logger.info(
            "notification_queued_for_web",
            user_id=str(user.id),
            draft_id=str(draft.id),
        )
        return {"status": "delivered_web", "channel": "web"}

    async def _dispatch_telegram_draft_notification(
        self,
        chat_id: int,
        user: UserRow,
        draft: DraftRow,
        version: DraftVersionRow,
        email: EmailRow,
        analysis: EmailAnalysisRow | None,
    ) -> dict[str, Any]:
        """Generate single-use callback tokens and send inline keyboard message via Telegram."""
        now = datetime.now(UTC)
        token_expiry = now + timedelta(minutes=15)

        # Single-use opaque tokens (16 hex chars each, <= 64 bytes total callback_data)
        t_approve = secrets.token_hex(8)
        t_revise = secrets.token_hex(8)
        t_reject = secrets.token_hex(8)
        t_shorter = secrets.token_hex(8)
        t_formal = secrets.token_hex(8)
        t_friendly = secrets.token_hex(8)
        t_firm = secrets.token_hex(8)

        # Store single-use tokens in callback_tokens table
        tokens_to_create = [
            (t_approve, "approve"),
            (t_revise, "revise"),
            (t_reject, "reject"),
            (t_shorter, "tone:shorter"),
            (t_formal, "tone:formal"),
            (t_friendly, "tone:friendly"),
            (t_firm, "tone:firm"),
        ]

        for token_val, action in tokens_to_create:
            await self.telegram_repo.create_callback_token(
                token=token_val,
                user_id=user.id,
                chat_id=chat_id,
                draft_id=draft.id,
                action=action,
                expires_at=token_expiry,
            )

        # Build inline keyboard
        inline_rows = [
            [
                {"text": "✅ Approve & Send", "callback_data": t_approve},
                {"text": "✍️ Revise", "callback_data": t_revise},
                {"text": "❌ Reject", "callback_data": t_reject},
            ],
            [
                {"text": "⚡ Shorter", "callback_data": t_shorter},
                {"text": "👔 Formal", "callback_data": t_formal},
                {"text": "😊 Friendly", "callback_data": t_friendly},
                {"text": "🎯 Direct", "callback_data": t_firm},
            ],
        ]

        # Smart Actions: check for meeting request
        smart_svc = SmartActionsService()
        meeting_info = smart_svc.extract_meeting_details(email)
        if meeting_info and meeting_info.get("calendar_url"):
            inline_rows.append(
                [
                    {
                        "text": "📅 Add to Google Calendar",
                        "url": meeting_info["calendar_url"],
                    }
                ]
            )

        frontend_url = self.settings.FRONTEND_BASE_URL.rstrip("/")
        if frontend_url and not ("localhost" in frontend_url or "127.0.0.1" in frontend_url):
            inline_rows.append(
                [
                    {
                        "text": "🌐 View in Web App",
                        "url": f"{frontend_url}/drafts/{draft.id}",
                    }
                ]
            )

        keyboard = {"inline_keyboard": inline_rows}

        # Extra Extractors: check for bills or phishing
        extra_svc = ExtraExtractorsService()
        extra_badges = []
        bill = extra_svc.extract_bill(email)
        if bill:
            extra_badges.append(
                f"🧾 <b>Bill:</b> {bill['currency']} {bill['amount']:.2f} (Due: {bill['due_date']})"
            )
        phishing = extra_svc.scan_phishing_indicators(email)
        if phishing:
            extra_badges.append(
                "⚠️ <b>Security Alert:</b> Suspicious links / phishing indicators detected!"
            )

        badges_text = ("\n" + "\n".join(extra_badges) + "\n") if extra_badges else ""

        # Build message body
        is_composed = "COMPOSED" in (email.labels or [])
        header_title = (
            "✍️ <b>Composed Draft Ready for Approval</b>"
            if is_composed
            else "📬 <b>New Draft Ready for Approval</b>"
        )
        address_line = (
            f"<b>To:</b> {escape_html(email.sender_email)}\n"
            if is_composed
            else f"<b>From:</b> {escape_html(email.sender)}\n"
        )
        version_label = (
            f"<b>Proposed Email (v{version.version}):</b>\n"
            if is_composed
            else f"<b>Proposed Reply (v{version.version}):</b>\n"
        )
        summary_text = analysis.summary if analysis else (email.snippet or "")
        summary_line = (
            f"<b>Notes:</b> {escape_html(email.body_clean[:150])}\n"
            if is_composed
            else f"<b>Summary:</b> {escape_html(summary_text)}\n"
        )

        msg_text = (
            f"{header_title}\n\n"
            f"{address_line}"
            f"<b>Subject:</b> {escape_html(email.subject)}\n"
            f"{summary_line}"
            f"{badges_text}\n"
            f"{version_label}"
            f"<blockquote>{escape_html(version.body)}</blockquote>"
        )

        sent_msg = await self.telegram_client.send_message(
            chat_id=chat_id,
            text=msg_text,
            reply_markup=keyboard,
        )

        return {
            "status": "delivered_telegram",
            "chat_id": chat_id,
            "message_id": sent_msg.get("message_id") if sent_msg else None,
        }
