"""Telegram bot service orchestrating account linking, commands, callbacks, and voice revisions."""

import contextlib
import hashlib
import hmac
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import DraftStatus, UserUpdate
from app.queue.redis import get_redis
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.digest_service import DigestService
from app.services.followup_service import FollowupService
from app.services.groq_service import GroqService
from app.services.inbox_qa import InboxQAService
from app.services.notification_service import NotificationService
from app.services.scheduled_email_service import (
    ScheduledEmailService,
    parse_schedule_time,
)
from app.services.scheduler_runner import run_direct_sync_cycle
from app.services.telegram_client import TelegramClient, escape_html
from app.services.unsubscribe_service import UnsubscribeService
from app.services.voice_service import (
    VOICE_PRESETS,
    is_voice_enabled,
    resolve_voice_name,
)

logger = structlog.get_logger("app.services.telegram")

_START_MESSAGE_IDS: dict[int, int] = {}
_PENDING_COMPOSE_STATES: dict[int, dict[str, Any]] = {}
_PENDING_SCHEDULE_STATES: dict[int, dict[str, Any]] = {}
_PENDING_SCHEDULE_EDIT_STATES: dict[int, dict[str, Any]] = {}
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def extract_email_address(text: str) -> str | None:
    match = EMAIL_REGEX.search(text)
    return match.group(0) if match else None


async def save_start_message_id(chat_id: int, message_id: int) -> None:
    """Track the 'Connect Gmail' message ID so it can be deleted after OAuth login."""
    _START_MESSAGE_IDS[chat_id] = message_id
    try:
        redis = get_redis()
        if redis:
            await redis.set(f"tg:start_msg:{chat_id}", str(message_id), ex=86400)
    except Exception:
        pass


async def get_start_message_id(chat_id: int) -> int | None:
    """Retrieve the 'Connect Gmail' message ID for cleanup."""
    try:
        redis = get_redis()
        if redis:
            val = await redis.get(f"tg:start_msg:{chat_id}")
            if val:
                return int(val)
    except Exception:
        pass
    return _START_MESSAGE_IDS.get(chat_id)


class TelegramService:
    """Processes Telegram webhooks, links accounts, executes callbacks, and handles voice notes."""

    def __init__(
        self,
        telegram_repo: TelegramRepo,
        user_repo: UserRepo,
        email_repo: EmailRepo,
        draft_repo: DraftRepo,
        approval_service: ApprovalService,
        notification_service: NotificationService,
        telegram_client: TelegramClient | None = None,
        groq_service: GroqService | None = None,
        auth_service: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.telegram_repo = telegram_repo
        self.user_repo = user_repo
        self.email_repo = email_repo
        self.draft_repo = draft_repo
        self.approval_service = approval_service
        self.notification_service = notification_service
        self.auth_service = auth_service or getattr(approval_service, "auth_service", None)
        self.settings = settings or get_settings()
        self.telegram_client = telegram_client or TelegramClient(
            telegram_repo=telegram_repo, settings=self.settings
        )
        self.groq_service = groq_service or GroqService(settings=self.settings)

    # -------------------------------------------------------------------------
    # Account Linking
    # -------------------------------------------------------------------------
    async def create_link_deep_link(self, user_id: UUID | str) -> str:
        """Generate single-use linking token and deep link URL."""
        raw_token = secrets.token_hex(24)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(minutes=10)

        await self.telegram_repo.create_link_token(
            token_hash=token_hash,
            user_id=user_id,
            expires_at=expires_at,
        )

        bot_username = self.settings.TELEGRAM_BOT_USERNAME
        return f"https://t.me/{bot_username}?start={raw_token}"

    def verify_webhook_secret(self, header_token: str | None) -> None:
        """Constant-time verification of Telegram webhook secret token."""
        expected = self.settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
        if not header_token or not hmac.compare_digest(header_token, expected):
            raise ForbiddenError("Invalid Telegram webhook secret header")

    # -------------------------------------------------------------------------
    # Webhook Dispatcher & Deduplication
    # -------------------------------------------------------------------------
    async def is_update_duplicate(self, update_id: int) -> bool:
        """Check and record update_id in Redis with 24-hour TTL. Returns True if duplicate."""
        redis = get_redis()
        if redis:
            dedup_key = f"tg:update:{update_id}"
            is_new = await redis.set(dedup_key, "1", ex=86400, nx=True)
            if not is_new:
                logger.debug("telegram_duplicate_update_skipped", update_id=update_id)
                return True
        return False

    async def process_webhook_update(self, update: dict[str, Any]) -> None:
        """Deduplicate updates by update_id and route to message or callback handler."""
        update_id = update.get("update_id")
        if not update_id:
            return

        if await self.is_update_duplicate(update_id):
            return

        # Route update
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    # -------------------------------------------------------------------------
    # Callback Query Handler (Single-Use Inline Buttons)
    # -------------------------------------------------------------------------
    async def _handle_callback_query(self, query: dict[str, Any]) -> None:
        token = query.get("data", "")
        chat_id = query.get("message", {}).get("chat", {}).get("id")
        message_id = query.get("message", {}).get("message_id")

        if not token or not chat_id:
            return

        # Direct per-user action callbacks (unsub, nudge, cancel_sched, sched_preview, sched_edit)
        if (
            token.startswith("unsub:")
            or token.startswith("nudge:")
            or token.startswith("cancel_sched:")
            or token.startswith("sched_preview:")
            or token.startswith("sched_edit:")
        ):
            link = await self.telegram_repo.get_link_by_chat_id(chat_id)
            if not link:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="✕ Your Telegram account is not linked.",
                )
                return
            if token.startswith("sched_preview:"):
                await self._handle_schedule_preview_callback(
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=link.user_id,
                    action=token.split(":", 1)[1],
                )
                return
            if token.startswith("sched_edit:"):
                parts = token.split(":", 2)
                if len(parts) == 3:
                    _, field_kind, sched_id = parts
                    _PENDING_SCHEDULE_EDIT_STATES[chat_id] = {
                        "user_id": link.user_id,
                        "scheduled_id": sched_id,
                        "field": field_kind,
                        "created_at": time.time(),
                    }
                    prompts = {
                        "content": (
                            "✎ <b>Edit Scheduled Content</b>\n\n"
                            "Reply with your new message/notes or revision instruction "
                            "(or /cancel to abort):"
                        ),
                        "recipient": (
                            "✎ <b>Edit Scheduled Recipient</b>\n\n"
                            "Reply with the new recipient email address "
                            "(e.g. <code>colleague@company.com</code>, or /cancel to abort):"
                        ),
                        "time": (
                            "◷ <b>Reschedule Send Time (IST)</b>\n\n"
                            "Reply with the new time (e.g. <code>in 45m</code>, "
                            "<code>21:30</code>, <code>tomorrow 09:00</code>, or /cancel):"
                        ),
                    }
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=prompts.get(field_kind, "Reply with the updated value:"),
                    )
                return
            action_kind, target_id = token.split(":", 1)
            if action_kind == "cancel_sched":
                sched_svc = ScheduledEmailService(settings=self.settings)
                cancelled = await sched_svc.cancel_scheduled_email(
                    user_id=link.user_id, scheduled_id=target_id
                )
                txt = (
                    "✓ <b>Scheduled email cancelled.</b>"
                    if cancelled
                    else "› Scheduled email was already sent or cancelled."
                )
                await self.telegram_client.send_message(chat_id=chat_id, text=txt)
                return
            if action_kind == "unsub":
                auth_svc = self.auth_service or AuthService(
                    user_repo=self.user_repo, settings=self.settings
                )
                unsub_svc = UnsubscribeService(
                    email_repo=self.email_repo, auth_service=auth_svc
                )
                res = await unsub_svc.unsubscribe_and_archive(
                    user_id=link.user_id, email_id=target_id
                )
                sender_lbl = escape_html(str(res.get("sender") or "Sender"))
                archived = res.get("archived_count", 0)
                status_note = (
                    "Unsubscribed &amp; archived"
                    if res.get("unsubscribed") and archived > 0
                    else (
                        "Unsubscribed"
                        if res.get("unsubscribed")
                        else "Archived from inbox"
                    )
                )
                msg = f"✓ <b>{status_note}:</b> {sender_lbl} ({archived} email(s) archived)."
                unsub_markup: dict[str, Any] | None = None
                if res.get("archive_permission_missing"):
                    base = self.settings.PUBLIC_BASE_URL.rstrip("/")
                    reconnect_url = f"{base}/api/v1/auth/google?chat_id={chat_id}"
                    msg += (
                        "\n\n› <i>Your current Gmail token doesn't have archive permission yet. "
                        "Tap below once to reconnect Gmail with archive access:</i>"
                    )
                    unsub_markup = {
                        "inline_keyboard": [
                            [{"text": "⟡ Reconnect Gmail", "url": reconnect_url}]
                        ]
                    }
                await self.telegram_client.send_message(
                    chat_id=chat_id, text=msg, reply_markup=unsub_markup
                )
            elif action_kind == "nudge":
                email = await self.email_repo.get_by_id(target_id)
                if not email or str(email.user_id) != str(link.user_id):
                    await self.telegram_client.send_message(
                        chat_id=chat_id, text="✕ Email not found."
                    )
                    return
                await self._execute_custom_draft_creation(
                    chat_id=chat_id,
                    user_id=link.user_id,
                    recipient=email.sender_email,
                    instruction=f"Write a polite follow-up nudge regarding: {email.subject}",
                )
            return

        # Atomically consume single-use callback token
        consumed = await self.telegram_repo.consume_callback_token(token, chat_id)
        if not consumed:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="⚠️ <i>This action button has expired or was already used.</i>",
            )
            return

        draft_id = consumed.draft_id
        user_id = consumed.user_id
        action = consumed.action

        try:
            if action == "approve":
                await self.approval_service.approve_and_send(draft_id, user_id)
                if message_id:
                    await self.telegram_client.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="✅ <b>Draft Approved and Sent via Gmail!</b>",
                        reply_markup=None,
                    )

            elif action == "reject":
                await self.approval_service.reject_draft(draft_id, user_id)
                if message_id:
                    await self.telegram_client.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="❌ <b>Draft Rejected and Cancelled.</b>",
                        reply_markup=None,
                    )

            elif action.startswith("tone:"):
                tone_name = action.split(":", 1)[1]
                revised_draft, new_version = await self.approval_service.revise_draft(
                    draft_id=draft_id,
                    user_id=user_id,
                    instruction="",
                    tone=tone_name,
                )
                email = await self.email_repo.get_by_id(revised_draft.email_id)
                if email and message_id:
                    # Update message with revised body and refresh inline buttons
                    await self._render_updated_draft_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        user_id=user_id,
                        draft=revised_draft,
                        version=new_version,
                        email=email,
                    )

            elif action == "revise":
                # Set pending interaction for custom instruction or voice note
                await self.telegram_repo.set_pending_interaction(
                    chat_id=chat_id,
                    user_id=user_id,
                    draft_id=draft_id,
                    interaction_type="revision_instruction",
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="✍️ <b>Please reply with revision instructions</b> (or send a voice note).",
                )

        except Exception as exc:
            logger.error(
                "telegram_callback_action_failed",
                action=action,
                draft_id=str(draft_id),
                error=str(exc),
            )
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"⚠️ <b>Action failed:</b> {escape_html(str(exc))}",
            )

    # -------------------------------------------------------------------------
    # Message Handler (Commands, Text Replies, Voice Notes)
    # -------------------------------------------------------------------------
    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return

        text = message.get("text", "").strip()
        voice = message.get("voice")

        # 1. Voice Note Handling
        if voice:
            await self._handle_voice_note(chat_id, voice)
            return

        # 2. Command Processing
        if text.startswith("/"):
            # Automatically delete messages containing sensitive API keys for privacy
            if text.startswith("/setkey") and message.get("message_id"):
                with contextlib.suppress(Exception):
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(message["message_id"])
                    )
            await self._handle_command(chat_id, text)
            return

        # 3. In-flight /create compose state processing (e.g. user answering recipient)
        compose_state = _PENDING_COMPOSE_STATES.get(chat_id)
        if compose_state and (time.time() - compose_state.get("created_at", 0) < 600):
            if "recipient" not in compose_state:
                found_email = extract_email_address(text)
                if found_email:
                    _PENDING_COMPOSE_STATES.pop(chat_id, None)
                    await self._execute_custom_draft_creation(
                        chat_id=chat_id,
                        user_id=compose_state["user_id"],
                        recipient=found_email,
                        instruction=compose_state["instruction"],
                    )
                    return
                else:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ <i>Please enter a valid recipient email address "
                            "(e.g. <code>colleague@example.com</code>). "
                            "Or send /cancel to abort.</i>"
                        ),
                    )
                    return
            elif "instruction" not in compose_state:
                _PENDING_COMPOSE_STATES.pop(chat_id, None)
                await self._execute_custom_draft_creation(
                    chat_id=chat_id,
                    user_id=compose_state["user_id"],
                    recipient=compose_state["recipient"],
                    instruction=text,
                )
                return

        # 3.4 In-flight post-schedule edit state (/manage -> edit content/recipient/time)
        edit_state = _PENDING_SCHEDULE_EDIT_STATES.get(chat_id)
        if edit_state and (time.time() - edit_state.get("created_at", 0) < 900):
            await self._apply_scheduled_edit(chat_id=chat_id, edit_state=edit_state, text=text)
            return

        # 3.5 In-flight /schedule 3-step & preview state (content -> recipient -> timing -> review)
        sched_state = _PENDING_SCHEDULE_STATES.get(chat_id)
        if sched_state and (time.time() - sched_state.get("created_at", 0) < 900):
            if sched_state.get("stage") == "review" and sched_state.get("awaiting_revision"):
                sched_state["awaiting_revision"] = False
                await self._revise_schedule_preview(
                    chat_id=chat_id,
                    user_id=sched_state["user_id"],
                    instruction=text,
                )
                return
            if "content" not in sched_state:
                sched_state["content"] = text
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "▸ <b>Step 2 of 3 — Recipient</b>\n\n"
                        "Who should receive this email?\n"
                        "Reply with their email address (e.g. <code>colleague@company.com</code>):"
                    ),
                )
                return
            if "recipient" not in sched_state:
                found_email = extract_email_address(text)
                if not found_email:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "✕ <i>Please enter a valid email address "
                            "(e.g. <code>colleague@company.com</code>), or /cancel to abort.</i>"
                        ),
                    )
                    return
                sched_state["recipient"] = found_email
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "▸ <b>Step 3 of 3 — Send Time (IST)</b>\n\n"
                        "When should I send it? Examples:\n"
                        "• <code>in 30m</code>  or  <code>in 2h</code>\n"
                        "• <code>21:30</code>  or  <code>9:30 pm</code>\n"
                        "• <code>tomorrow 09:00</code>  or  <code>2026-11-26 14:00</code>"
                    ),
                )
                return
            if "scheduled_at" not in sched_state:
                user_obj = await self.user_repo.get_by_id(sched_state["user_id"])
                user_tz = (
                    user_obj.timezone
                    if user_obj and user_obj.timezone and user_obj.timezone != "UTC"
                    else "Asia/Kolkata"
                )
                parsed_dt = parse_schedule_time(text, user_timezone=user_tz)
                if not parsed_dt:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "✕ <i>Could not understand that time or it is in the past.</i>\n"
                            "Try <code>in 30m</code>, <code>21:30</code>, "
                            "<code>tomorrow 09:00</code>, or /cancel to abort."
                        ),
                    )
                    return

                await self._finalize_scheduled_email(
                    chat_id=chat_id,
                    user_id=sched_state["user_id"],
                    recipient=sched_state["recipient"],
                    notes=sched_state["content"],
                    scheduled_at=parsed_dt,
                    user_tz=user_tz,
                )
                return

        # 4. Pending Interaction Processing (e.g. revision text reply)
        pending = await self.telegram_repo.get_and_clear_pending_interaction(chat_id)
        if pending and pending.type == "revision_instruction":
            await self._apply_text_revision(
                chat_id=chat_id,
                user_id=pending.user_id,
                draft_id=pending.draft_id,
                instruction=text,
            )
            return

        # 4. Natural Language Inbox Q&A for linked users
        link = await self.telegram_repo.get_link_by_chat_id(chat_id)
        if link and len(text) >= 3:
            await self._handle_inbox_qa(chat_id, link.user_id, text)
            return

        # Fallback greeting
        await self.telegram_client.send_message(
            chat_id=chat_id,
            text=(
                "Type /help to see all commands, /pending for drafts, "
                "or simply ask me any question about your emails!"
            ),
        )

    async def _handle_inbox_qa(self, chat_id: int, user_id: UUID | str, query: str) -> None:
        """Answer natural language question about user's inbox using Groq LLM."""
        status_msg = await self.telegram_client.send_message(
            chat_id=chat_id,
            text="🔍 <i>Searching your emails...</i>",
        )
        msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
        try:
            qa_svc = InboxQAService(
                email_repo=self.email_repo,
                settings=self.settings,
                auth_service=self.auth_service,
            )
            result = await qa_svc.answer_question(user_id=user_id, query=query)
            answer = result.get("answer", "")
            citations = result.get("citations", [])

            resp_text = f"💡 <b>Answer:</b>\n\n{escape_html(answer)}"
            if citations:
                resp_text += "\n\n<b>Sources:</b>"
                for c in citations[:3]:
                    subj = escape_html(c.get("subject", "Email"))
                    sender = escape_html(c.get("sender", ""))
                    resp_text += f"\n• <b>{subj}</b> (from {sender})"

            if msg_id:
                await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)

            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=resp_text,
            )
        except Exception as exc:
            if msg_id:
                await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)
            logger.error("telegram_qa_failed", query=query, error=str(exc))
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"⚠️ <b>Could not answer query:</b> {escape_html(str(exc))}",
            )

    # -------------------------------------------------------------------------
    # Command Router
    # -------------------------------------------------------------------------
    async def _handle_command(self, chat_id: int, command_text: str) -> None:
        parts = command_text.split()
        cmd = parts[0].lower()

        # Check existing chat linkage
        link = await self.telegram_repo.get_link_by_chat_id(chat_id)

        # Handle /start with or without token
        if cmd == "/start":
            if len(parts) > 1:
                token_param = parts[1].strip()
                token_hash = hashlib.sha256(token_param.encode("utf-8")).hexdigest()
                user_id = await self.telegram_repo.consume_link_token(token_hash)
                if user_id:
                    await self.telegram_repo.link_chat(user_id=user_id, chat_id=chat_id)
                    user = await self.user_repo.get_by_id(user_id)
                    has_key = bool(user and user.prefs and user.prefs.get("groq_api_key_encrypted"))
                    if not has_key:
                        await self.telegram_client.send_message(
                            chat_id=chat_id,
                            text=(
                                "🎉 <b>Account successfully linked!</b>\n\n"
                                "🔑 <b>Final Step: Set your Groq API Key</b>\n"
                                "To enable AI email analysis and reply drafts on your own quota, "
                                "enter your Groq API key:\n\n"
                                "<code>/setkey gsk_your_key_here</code>\n\n"
                                "<i>(Get a free key at https://console.groq.com/keys)</i>\n"
                                "<i>Your key is encrypted with AES-256 and your message is "
                                "deleted immediately for privacy.</i>"
                            ),
                        )
                    else:
                        await self.telegram_client.send_message(
                            chat_id=chat_id,
                            text=(
                                "🎉 <b>Account successfully linked!</b>\n\n"
                                "You will receive email summaries and draft approvals here.\n"
                                "Type /help to view all bot commands."
                            ),
                        )
                else:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text="❌ <b>Link expired or invalid.</b> Please generate a new link.",
                    )
            elif link and getattr(link, "is_active", False) is True:
                user = await self.user_repo.get_by_id(link.user_id)
                user_email = user.email if user else "your Gmail"
                user_name = user.name if user and user.name else "there"
                has_key = bool(user and user.prefs and user.prefs.get("groq_api_key_encrypted"))
                key_line = (
                    "• 🔑 /setkey — Set personal Groq API key\n"
                    if not has_key
                    else "• 🔑 /mykey — View Groq API key status\n"
                )
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"👋 <b>Welcome back, {escape_html(user_name)}!</b>\n\n"
                        f"Your Telegram is already linked to "
                        f"<code>{escape_html(user_email)}</code>.\n\n"
                        f"<b>Quick Actions:</b>\n"
                        f"• /pending — Review drafts awaiting approval\n"
                        f"• /sync — Pull new emails immediately\n"
                        f"• /digest — Generate your daily digest\n"
                        f"• /urgent — View urgent unread emails\n"
                        f"{key_line}"
                        f"• /help — View all commands\n\n"
                        f"<i>You can also ask me any question about your inbox directly!</i>"
                    ),
                )
            else:
                base = self.settings.PUBLIC_BASE_URL.rstrip("/")
                connect_url = f"{base}/api/v1/auth/google?chat_id={chat_id}"
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "⟡ Connect Gmail",
                                "url": connect_url,
                            }
                        ]
                    ]
                }
                sent_msg = await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "👋 <b>Welcome to Email Agent!</b>\n\n"
                        "I am your personal AI email assistant. I summarize unread emails, "
                        "draft replies in your voice, and send only with your approval.\n\n"
                        "To get started, please connect your Gmail account:"
                    ),
                    reply_markup=reply_markup,
                )
                if isinstance(sent_msg, dict) and sent_msg.get("message_id"):
                    await save_start_message_id(chat_id, int(sent_msg["message_id"]))
            return

        # Authenticate existing linked user for subsequent commands
        if not link:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="⚠️ Your Telegram account is not linked. Please connect via the web app.",
            )
            return

        user_id = link.user_id

        if cmd == "/setkey":
            if len(parts) < 2:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "ℹ️ <b>How to set your Groq API key:</b>\n\n"
                        "Send: <code>/setkey gsk_your_groq_key_here</code>\n\n"
                        "<i>(You can get a free key in 30 seconds at https://console.groq.com/keys)</i>\n"
                        "<i>Your key is encrypted with AES-256 and your message is "
                        "automatically deleted for privacy.</i>"
                    ),
                )
                return

            raw_key = parts[1].strip()
            status_msg = await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🔄 <i>Validating and securely encrypting your Groq API key...</i>",
            )
            status_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None

            try:
                auth_svc = self.auth_service or getattr(self.approval_service, "auth_service", None)
                if not auth_svc:
                    auth_svc = AuthService(user_repo=self.user_repo, settings=self.settings)

                masked = await auth_svc.set_user_groq_api_key(user_id, raw_key)
                if status_id:
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(status_id)
                    )

                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "✅ <b>Groq API Key Configured Successfully!</b>\n\n"
                        f"• Key: <code>{masked}</code>\n"
                        f"• Encryption: <b>AES-256-GCM</b>\n\n"
                        "All email summaries, triage analysis, and reply drafts will now run "
                        "on your dedicated Groq quota.\n"
                        "Use /mykey to view status or /removekey to delete it."
                    ),
                )
            except Exception as exc:
                if status_id:
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(status_id)
                    )
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"❌ <b>Failed to set Groq key:</b> {escape_html(str(exc))}",
                )

        elif cmd == "/mykey":
            auth_svc = self.auth_service or getattr(self.approval_service, "auth_service", None)
            if not auth_svc:
                auth_svc = AuthService(user_repo=self.user_repo, settings=self.settings)

            key_status = await auth_svc.get_user_groq_key_status(user_id)
            if key_status.get("has_key"):
                masked = key_status.get("masked_key", "Configured")
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "🔑 <b>Your Groq API Key:</b>\n\n"
                        f"• Status: <b>Active</b>\n"
                        f"• Key: <code>{masked}</code>\n\n"
                        "To update it, send: <code>/setkey gsk_...</code>\n"
                        "To remove it, send: /removekey"
                    ),
                )
            else:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "ℹ️ <b>No Personal Groq API Key Configured</b>\n\n"
                        "Currently using default system tier.\n\n"
                        "To connect your own dedicated Groq quota, send:\n"
                        "<code>/setkey gsk_your_key_here</code>\n\n"
                        "<i>(Get a free key at https://console.groq.com/keys)</i>"
                    ),
                )

        elif cmd == "/removekey":
            auth_svc = self.auth_service or getattr(self.approval_service, "auth_service", None)
            if not auth_svc:
                auth_svc = AuthService(user_repo=self.user_repo, settings=self.settings)
            await auth_svc.remove_user_groq_api_key(user_id)
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🗑️ <b>Personal Groq API Key Removed.</b> Reverted to system default.",
            )

        elif cmd == "/disconnect":
            await self.telegram_repo.deactivate_chat(chat_id)
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🔌 <b>Telegram disconnected.</b> Notifications have been stopped.",
            )

        elif cmd == "/reconnect":
            base = self.settings.PUBLIC_BASE_URL.rstrip("/")
            connect_url = f"{base}/api/v1/auth/google?chat_id={chat_id}"
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=(
                    "🔑 <b>Reconnect Gmail</b>\n\n"
                    "Tap the button below to re-authorize your Gmail account and grant "
                    "updated permissions (including archiving):"
                ),
                reply_markup={
                    "inline_keyboard": [[{"text": "⟡ Reconnect Gmail", "url": connect_url}]]
                },
            )

        elif cmd == "/create":
            rest = command_text[len(cmd) :].strip()
            if not rest:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "✍️ <b>Compose a New Email Draft</b>\n\n"
                        "Give me the recipient and your rough notes, and I will compose a "
                        "polished draft for your review.\n\n"
                        "<b>Format:</b>\n"
                        "<code>/create &lt;recipient_email&gt; &lt;your notes&gt;</code>\n\n"
                        "<b>Example:</b>\n"
                        "<code>/create sarah@company.com Review design draft by 3pm</code>\n\n"
                        "<i>(You can also just send: <code>/create Tell her I'll be late</code> "
                        "and I will ask you for the email address!)</i>"
                    ),
                )
                return

            recipient = extract_email_address(rest)
            if not recipient:
                # User provided instructions but missed the recipient
                _PENDING_COMPOSE_STATES[chat_id] = {
                    "instruction": rest,
                    "user_id": user_id,
                    "created_at": time.time(),
                }
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "📧 <b>Recipient Needed</b>\n\n"
                        "Who should I send this email to?\n\n"
                        "Please reply with the recipient's email address "
                        "(e.g. <code>colleague@example.com</code>):"
                    ),
                )
                return

            # Strip recipient from instructions
            cleaned_instruction = EMAIL_REGEX.sub("", rest).strip()
            cleaned_instruction = re.sub(
                r"(?i)^(to|recipient|sender)\s*:\s*", "", cleaned_instruction
            ).strip()

            if not cleaned_instruction:
                # User gave only recipient
                _PENDING_COMPOSE_STATES[chat_id] = {
                    "recipient": recipient,
                    "user_id": user_id,
                    "created_at": time.time(),
                }
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✍️ <b>What would you like to say to "
                        f"<code>{escape_html(recipient)}</code>?</b>\n\n"
                        "Reply here with your rough notes or instructions, "
                        "and I will draft the email for you."
                    ),
                )
                return

            await self._execute_custom_draft_creation(
                chat_id=chat_id,
                user_id=user_id,
                recipient=recipient,
                instruction=cleaned_instruction,
            )

        elif cmd == "/cancel":
            if chat_id in _PENDING_COMPOSE_STATES:
                _PENDING_COMPOSE_STATES.pop(chat_id, None)
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✕ Draft creation cancelled."
                )
            elif chat_id in _PENDING_SCHEDULE_STATES or chat_id in _PENDING_SCHEDULE_EDIT_STATES:
                _PENDING_SCHEDULE_STATES.pop(chat_id, None)
                _PENDING_SCHEDULE_EDIT_STATES.pop(chat_id, None)
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✕ Email scheduling cancelled."
                )
            else:
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="› No active composition or schedule in progress."
                )

        elif cmd == "/schedule":
            rest = command_text[len(cmd) :].strip()
            if rest.lower() == "list":
                await self._send_scheduled_list(chat_id, user_id)
                return
            _PENDING_SCHEDULE_STATES[chat_id] = {
                "user_id": user_id,
                "created_at": time.time(),
            }
            if rest:
                _PENDING_SCHEDULE_STATES[chat_id]["content"] = rest
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "◷ <b>Schedule Email (2 of 3 — Recipient)</b>\n\n"
                        "Content saved. Who should receive this email?\n"
                        "Reply with their email address (e.g. <code>colleague@company.com</code>):"
                    ),
                )
            else:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "◷ <b>Schedule an Email (Step 1 of 3 — Content)</b>\n\n"
                        "What should this email say? Reply with your message or rough notes.\n"
                        "<i>(Use /scheduled to view pending emails, or /cancel to abort.)</i>"
                    ),
                )

        elif cmd in ("/scheduled", "/manage"):
            await self._send_scheduled_list(chat_id, user_id)

        elif cmd == "/pending":
            drafts = await self.draft_repo.list_by_user(
                user_id=user_id, status=DraftStatus.PENDING_APPROVAL.value, limit=5
            )
            if not drafts:
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✨ No pending drafts waiting for approval!"
                )
                return

            for d in drafts:
                versions = await self.draft_repo.get_versions(d.id)
                email = await self.email_repo.get_by_id(d.email_id)
                if email and versions:
                    await self.notification_service.notify_new_draft(
                        draft=d,
                        version=versions[-1],
                        email=email,
                    )

        elif cmd == "/urgent":
            emails = await self.email_repo.list_by_user(user_id=user_id, limit=5)
            urgent = [
                e
                for e in emails
                if "urgent" in (e.subject or "").lower() or "important" in e.labels
            ]
            if not urgent:
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="🟢 No urgent unread emails detected."
                )
            else:
                lines = [
                    f"• <b>{escape_html(e.sender)}:</b> {escape_html(e.subject)}" for e in urgent
                ]
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="🚨 <b>Urgent Emails:</b>\n\n" + "\n".join(lines),
                )

        elif cmd == "/snooze":
            hours = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
            snooze_until = datetime.now(UTC) + timedelta(hours=hours)
            await self.user_repo.upsert_notification_prefs(
                user_id, {"snoozed_until": snooze_until.isoformat()}
            )
            time_str = snooze_until.strftime("%H:%M UTC")
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"🔕 Notifications snoozed for {hours} hours (until {time_str}).",
            )

        elif cmd == "/mute":
            snooze_until = datetime.now(UTC) + timedelta(hours=24)
            await self.user_repo.upsert_notification_prefs(
                user_id, {"snoozed_until": snooze_until.isoformat()}
            )
            await self.telegram_client.send_message(
                chat_id=chat_id, text="🔕 Notifications muted for 24 hours."
            )

        elif cmd == "/settings":
            prefs = await self.user_repo.get_notification_prefs(user_id)
            channel = prefs.channel if prefs else "telegram"
            quiet = (
                f"{prefs.quiet_start} - {prefs.quiet_end}"
                if prefs and prefs.quiet_start
                else "Not configured"
            )
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=(
                    f"⚙️ <b>Notification Settings:</b>\n\n"
                    f"• <b>Channel:</b> {channel}\n"
                    f"• <b>Quiet Hours:</b> {quiet}\n"
                    f"• <b>Minimal Mode:</b> {getattr(prefs, 'minimal_mode', False)}"
                ),
            )

        elif cmd == "/ask":
            query = " ".join(parts[1:]).strip()
            if not query:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "ℹ️ <i>Please provide a question. Example:</i> "
                        "<code>/ask when is my meeting with Alex?</code>"
                    ),
                )
            else:
                await self._handle_inbox_qa(chat_id, user_id, query)

        elif cmd == "/sync":
            status_msg = await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🔄 <i>Synchronizing your inbox from Gmail...</i>",
            )
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
            try:
                cycle_res = await run_direct_sync_cycle(target_user_id=str(user_id))
                new_synced = cycle_res.get("new_emails_synced", 0)
                drafts_created = cycle_res.get("drafts_created", 0)
                if msg_id:
                    await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)

                lines = [
                    "✅ <b>Inbox Sync Complete!</b>\n",
                    f"• New emails synced: <b>{new_synced}</b>",
                    f"• Reply drafts ready: <b>{drafts_created}</b>",
                ]
                if drafts_created > 0:
                    lines.append("\nUse /pending to review and approve drafts.")

                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                )
            except Exception as exc:
                if msg_id:
                    await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.error("telegram_sync_command_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Sync failed: {escape_html(str(exc))}",
                )

        elif cmd == "/digest":
            status_msg = await self.telegram_client.send_message(
                chat_id=chat_id,
                text="📊 <i>Compiling your daily email digest...</i>",
            )
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
            try:
                digest_svc = DigestService(
                    user_repo=self.user_repo,
                    email_repo=self.email_repo,
                    draft_repo=self.draft_repo,
                    telegram_repo=self.telegram_repo,
                    auth_service=self.auth_service,
                    settings=self.settings,
                )
                res = await digest_svc.generate_and_dispatch_digest(user_id, force=True)
                if msg_id:
                    await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)
                if res.get("status") == "skipped":
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=f"ℹ️ Digest skipped: {res.get('reason', 'no recent activity')}",
                    )
            except Exception as exc:
                if msg_id:
                    await self.telegram_client.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.error("telegram_digest_command_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Digest failed: {escape_html(str(exc))}",
                )

        elif cmd in ("/followups", "/commitments"):
            try:
                followup_svc = FollowupService()
                commitments = await followup_svc.list_open_commitments(user_id)
                if not commitments:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text="✨ No open promises or commitments detected in recent sent emails!",
                    )
                else:
                    lines = []
                    for c in commitments[:5]:
                        due = c.get("due_date", "Pending")
                        promise = c.get("promise", "")
                        lines.append(f"• <b>{escape_html(promise)}</b> (Due: {due})")
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text="📋 <b>Tracked Commitments & Promises:</b>\n\n" + "\n".join(lines),
                    )
            except Exception as exc:
                logger.error("telegram_followups_command_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Could not list commitments: {escape_html(str(exc))}",
                )

        elif cmd == "/radar":
            try:
                radar = await FollowupService().get_radar_summary(
                    user_id=user_id, email_repo=self.email_repo, limit=5
                )
                w_you = radar.get("waiting_on_you", [])
                w_others = radar.get("waiting_on_others", [])
                if not w_you and not w_others:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "🎯 <b>Decision &amp; Commitment Radar</b>\n\n"
                            "✨ All clear! No pending replies or open commitments."
                        ),
                    )
                    return

                lines = ["◈ <b>Decision &amp; Commitment Radar</b>\n"]
                buttons: list[list[dict[str, str]]] = []
                if w_you:
                    lines.append("▸ <b>People Waiting on You:</b>")
                    for item in w_you:
                        lines.append(
                            f"• <b>{escape_html(item['sender'])}</b>: "
                            f"{escape_html(item['subject'])}"
                        )
                    lines.append("")
                if w_others:
                    lines.append("▸ <b>Open Commitments &amp; Follow-ups:</b>")
                    for item in w_others:
                        lines.append(
                            f"• {escape_html(item['label'])} (Due: {escape_html(item['due_date'])})"
                        )
                        if item.get("email_id"):
                            buttons.append(
                                [
                                    {
                                        "text": f"▸ Draft Nudge: {item['label'][:24]}",
                                        "callback_data": f"nudge:{item['email_id']}",
                                    }
                                ]
                            )
                radar_markup = {"inline_keyboard": buttons} if buttons else None
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    reply_markup=radar_markup,
                )
            except Exception as exc:
                logger.error("telegram_radar_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Could not load radar: {escape_html(str(exc))}",
                )

        elif cmd == "/unsubscribe":
            try:
                auth_svc = self.auth_service or AuthService(
                    user_repo=self.user_repo, settings=self.settings
                )
                unsub_svc = UnsubscribeService(
                    email_repo=self.email_repo, auth_service=auth_svc
                )
                candidates = await unsub_svc.list_candidates(user_id=user_id, limit=5)
                if not candidates:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text="✨ No newsletter or promo senders with unsubscribe links found.",
                    )
                    return

                lines = ["◈ <b>One-Tap Newsletter &amp; Promo Purge</b>\n"]
                buttons = []
                for c in candidates:
                    cnt = len(c.get("gmail_msg_ids", []))
                    lines.append(
                        f"• <b>{escape_html(c['sender'])}</b> "
                        f"(<code>{escape_html(c['sender_email'])}</code>) — {cnt} email(s)"
                    )
                    buttons.append(
                        [
                            {
                                "text": f"✕ Unsubscribe & Archive: {c['sender'][:22]}",
                                "callback_data": f"unsub:{c['email_id']}",
                            }
                        ]
                    )
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    reply_markup={"inline_keyboard": buttons},
                )
            except Exception as exc:
                logger.error("telegram_unsubscribe_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Could not list newsletters: {escape_html(str(exc))}",
                )

        elif cmd == "/find":
            query = " ".join(parts[1:]).strip()
            if not query:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="ℹ️ <i>Usage:</i> <code>/find flight ticket</code>",
                )
                return
            status_msg = await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🔍 <i>Searching your encrypted emails...</i>",
            )
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
            try:
                qa_svc = InboxQAService(
                    email_repo=self.email_repo,
                    settings=self.settings,
                    auth_service=self.auth_service,
                )
                hits = await qa_svc.search_emails(user_id=user_id, query=query, limit=5)
                if msg_id:
                    with contextlib.suppress(Exception):
                        await self.telegram_client.delete_message(
                            chat_id=chat_id, message_id=int(msg_id)
                        )
                if not hits:
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=f"🔍 No emails found matching <b>{escape_html(query)}</b>.",
                    )
                    return
                lines = [f'🔍 <b>Search Results for "{escape_html(query)}":</b>\n']
                for h in hits:
                    date_part = (h.get("received_at") or "")[:10]
                    lines.append(
                        f"• <b>{escape_html(h['subject'])}</b>\n"
                        f"  From: {escape_html(h['sender'])} ({date_part})\n"
                        f"  <i>{escape_html(h.get('summary') or '')}</i>"
                    )
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                )
            except Exception as exc:
                if msg_id:
                    with contextlib.suppress(Exception):
                        await self.telegram_client.delete_message(
                            chat_id=chat_id, message_id=int(msg_id)
                        )
                logger.error("telegram_find_failed", query=query, error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <b>Search failed:</b> {escape_html(str(exc))}",
                )

        elif cmd == "/voice":
            arg = parts[1].strip().lower() if len(parts) > 1 else ""
            user = await self.user_repo.get_by_id(user_id)
            user_prefs: dict[str, Any] = dict(user.prefs) if user and user.prefs else {}

            if arg in ("on", "off"):
                enabled = arg == "on"
                user_prefs["voice_briefing"] = enabled
                await self.user_repo.update_user(user_id, UserUpdate(prefs=user_prefs))
                state_lbl = "enabled ✓" if enabled else "disabled ✕"
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"◉ Morning voice briefing is now <b>{state_lbl}</b> for your account.",
                )
                return

            if arg == "list":
                current_voice = resolve_voice_name(user_prefs)
                enabled = is_voice_enabled(user_prefs)
                lines = [
                    "◉ <b>Voice Briefing Settings</b>\n",
                    f"• Status: <b>{'Enabled' if enabled else 'Disabled'}</b>",
                    f"• Active Voice: <code>{escape_html(current_voice)}</code>\n",
                    "▸ <b>Available Presets:</b>",
                ]
                for key, (v_id, desc) in VOICE_PRESETS.items():
                    marker = " ✓" if v_id == current_voice else ""
                    lines.append(f"• <code>/voice {key}</code> — {desc}{marker}")
                lines.append(
                    "\n<i>Toggle with</i> <code>/voice on</code> "
                    "<i>or</i> <code>/voice off</code>."
                )
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="\n".join(lines)
                )
                return

            if arg in VOICE_PRESETS:
                v_id, desc = VOICE_PRESETS[arg]
                user_prefs["voice_name"] = arg
                await self.user_repo.update_user(user_id, UserUpdate(prefs=user_prefs))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"◉ Voice updated to <b>{desc}</b> (<code>{v_id}</code>).",
                )
                return

            if arg:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ Unknown voice option. Use <code>/voice list</code> to see available "
                        "voices, <code>/voice on|off</code>, or <code>/voice</code> to play now."
                    ),
                )
                return

            # No args: send BOTH text digest and voice briefing right now
            status_msg = await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🎙️ <i>Preparing your text + voice briefing...</i>",
            )
            msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
            try:
                digest_svc = DigestService(
                    user_repo=self.user_repo,
                    email_repo=self.email_repo,
                    draft_repo=self.draft_repo,
                    telegram_repo=self.telegram_repo,
                    telegram_client=self.telegram_client,
                    auth_service=self.auth_service,
                    settings=self.settings,
                )
                await digest_svc.generate_and_dispatch_digest(
                    user_id, force=True, include_voice=True
                )
            finally:
                if msg_id:
                    with contextlib.suppress(Exception):
                        await self.telegram_client.delete_message(
                            chat_id=chat_id, message_id=int(msg_id)
                        )

        elif cmd == "/help":
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=(
                    "◈ <b>Email Agent — Command Reference</b>\n\n"
                    "<b>▸ Compose &amp; Schedule</b>\n"
                    "• <code>/create &lt;to&gt; &lt;notes&gt;</code> — "
                    "Draft a new email for review\n"
                    "• <code>/schedule</code> — "
                    "Guided 3-step email scheduler with preview &amp; revise\n"
                    "• <code>/manage</code> (or <code>/scheduled</code>) — "
                    "Edit, reschedule, or cancel pending emails\n"
                    "• <code>/pending</code> — Review and approve waiting reply drafts\n"
                    "• <code>/cancel</code> — Cancel an active /create or /schedule flow\n\n"
                    "<b>▸ Inbox Intelligence</b>\n"
                    "• <code>/ask &lt;question&gt;</code> — Ask AI anything about your emails\n"
                    "• <code>/find &lt;keyword&gt;</code> — Search your encrypted email archive\n"
                    "• <code>/radar</code> — See who is waiting on you &amp; open follow-ups\n"
                    "• <code>/followups</code> — List tracked promises &amp; commitments\n"
                    "• <code>/urgent</code> — Show high-priority unread emails\n"
                    "• <code>/unsubscribe</code> — One-tap newsletter &amp; promo purge\n\n"
                    "<b>▸ Sync, Digests &amp; Voice</b>\n"
                    "• <code>/sync</code> — Pull new emails from Gmail now\n"
                    "• <code>/digest</code> — Compile and send email digest now\n"
                    "• <code>/voice</code> — Send text + audio briefing now\n"
                    "  <i>Options:</i> <code>/voice list</code>, <code>/voice &lt;name&gt;</code>, "
                    "<code>/voice on|off</code>\n\n"
                    "<b>▸ Account &amp; Security</b>\n"
                    "• <code>/setkey &lt;gsk_...&gt;</code> — Set personal Groq API key\n"
                    "• <code>/mykey</code> / <code>/removekey</code> — Check or remove Groq key\n"
                    "• <code>/snooze &lt;hours&gt;</code> / <code>/mute</code> — Pause alerts\n"
                    "• <code>/settings</code> — View notification preferences\n"
                    "• <code>/reconnect</code> — Re-authorize Gmail permissions\n"
                    "• <code>/disconnect</code> — Unlink this Telegram chat\n\n"
                    "<i>Tip: Send any plain message to ask about your inbox, "
                    "or send a voice note to revise a draft.</i>"
                ),
            )

    # -------------------------------------------------------------------------
    # Voice Note Processing
    # -------------------------------------------------------------------------
    async def _handle_voice_note(self, chat_id: int, voice: dict[str, Any]) -> None:
        file_id = voice.get("file_id")
        if not file_id:
            return

        link = await self.telegram_repo.get_link_by_chat_id(chat_id)
        if not link:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="⚠️ Please link your account first via the web app.",
            )
            return

        file_meta = await self.telegram_client.get_file(file_id)
        file_path = file_meta.get("file_path") if file_meta else None
        if not file_path:
            await self.telegram_client.send_message(
                chat_id=chat_id, text="⚠️ Could not retrieve voice audio file."
            )
            return

        audio_bytes = await self.telegram_client.download_file(file_path)
        user_groq_key: str | None = None
        if self.auth_service and hasattr(self.auth_service, "get_user_groq_api_key"):
            try:
                user_groq_key = await self.auth_service.get_user_groq_api_key(link.user_id)
            except Exception:
                user_groq_key = None
        transcript = await self.groq_service.transcribe_audio(
            audio_bytes, filename="voice.ogg", api_key=user_groq_key
        )

        # Check for active pending interaction
        pending = await self.telegram_repo.get_and_clear_pending_interaction(chat_id)
        if pending and pending.type == "revision_instruction":
            esc_tr = escape_html(transcript)
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f'🎙️ <i>Heard instruction:</i> "{esc_tr}"\n\nApplying revision...',
            )
            await self._apply_text_revision(
                chat_id=chat_id,
                user_id=pending.user_id,
                draft_id=pending.draft_id,
                instruction=transcript,
            )
        else:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f'🎙️ <b>Transcribed Voice Note:</b>\n\n"{escape_html(transcript)}"',
            )

    async def _apply_text_revision(
        self, chat_id: int, user_id: UUID, draft_id: UUID, instruction: str
    ) -> None:
        try:
            revised_draft, new_version = await self.approval_service.revise_draft(
                draft_id=draft_id,
                user_id=user_id,
                instruction=instruction,
            )
            email = await self.email_repo.get_by_id(revised_draft.email_id)
            if email:
                await self.notification_service.notify_new_draft(
                    draft=revised_draft,
                    version=new_version,
                    email=email,
                )
        except Exception as exc:
            logger.error("telegram_text_revision_failed", draft_id=str(draft_id), error=str(exc))
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"⚠️ Revision failed: {escape_html(str(exc))}",
            )

    async def _execute_custom_draft_creation(
        self,
        chat_id: int,
        user_id: UUID | str,
        recipient: str,
        instruction: str,
    ) -> None:
        """Compose a brand new outbound email draft and present it via the draft pipeline."""
        status_msg = await self.telegram_client.send_message(
            chat_id=chat_id,
            text=f"✍️ <i>Composing email draft for <code>{escape_html(recipient)}</code>...</i>",
        )
        msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
        try:
            draft, email = await self.approval_service.create_custom_draft(
                user_id=user_id,
                recipient=recipient,
                user_instruction=instruction,
            )
            versions = await self.draft_repo.get_versions(draft.id)
            if msg_id:
                await self.telegram_client.delete_message(chat_id=chat_id, message_id=int(msg_id))

            if versions:
                await self.notification_service.notify_new_draft(
                    draft=draft,
                    version=versions[-1],
                    email=email,
                )
        except Exception as exc:
            if msg_id:
                await self.telegram_client.delete_message(chat_id=chat_id, message_id=int(msg_id))
            logger.error("execute_custom_draft_creation_failed", error=str(exc))
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"⚠️ Failed to compose draft: {escape_html(str(exc))}",
            )

    async def _render_updated_draft_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: UUID,
        draft: Any,
        version: Any,
        email: Any,
    ) -> None:
        """Edit an existing message to display the newly revised draft."""
        now = datetime.now(UTC)
        token_expiry = now + timedelta(minutes=15)

        t_approve = secrets.token_hex(8)
        t_revise = secrets.token_hex(8)
        t_reject = secrets.token_hex(8)
        t_shorter = secrets.token_hex(8)
        t_formal = secrets.token_hex(8)
        t_friendly = secrets.token_hex(8)
        t_firm = secrets.token_hex(8)

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
                user_id=user_id,
                chat_id=chat_id,
                draft_id=draft.id,
                action=action,
                expires_at=token_expiry,
            )

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

        msg_text = (
            f"📬 <b>Revised Draft Ready (v{version.version})</b>\n\n"
            f"<b>From:</b> {escape_html(email.sender)}\n"
            f"<b>Subject:</b> {escape_html(email.subject)}\n\n"
            f"<b>Proposed Reply:</b>\n"
            f"<blockquote>{escape_html(version.body)}</blockquote>"
        )

        await self.telegram_client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=msg_text,
            reply_markup=keyboard,
        )

    async def _finalize_scheduled_email(
        self,
        chat_id: int,
        user_id: UUID | str,
        recipient: str,
        notes: str,
        scheduled_at: datetime,
        user_tz: str = "Asia/Kolkata",
    ) -> None:
        """Compose a preview for the scheduled email and ask user to Accept, Revise, or Cancel."""
        status_msg = await self.telegram_client.send_message(
            chat_id=chat_id,
            text="◷ <i>Composing scheduled email preview...</i>",
        )
        msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None
        try:
            user = await self.user_repo.get_by_id(user_id)
            user_groq_key: str | None = None
            if self.auth_service and hasattr(self.auth_service, "get_user_groq_api_key"):
                with contextlib.suppress(Exception):
                    user_groq_key = await self.auth_service.get_user_groq_api_key(user_id)

            sched_svc = ScheduledEmailService(
                settings=self.settings, groq_service=self.groq_service
            )
            subject, body = await sched_svc.compose_subject_and_body(
                recipient=recipient,
                notes=notes,
                user_name=user.name if user else None,
                api_key=user_groq_key,
            )
            if msg_id:
                with contextlib.suppress(Exception):
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(msg_id)
                    )

            _PENDING_SCHEDULE_STATES[chat_id] = {
                "user_id": user_id,
                "stage": "review",
                "recipient": recipient,
                "notes": notes,
                "subject": subject,
                "body": body,
                "scheduled_at": scheduled_at,
                "user_tz": user_tz,
                "awaiting_revision": False,
                "created_at": time.time(),
            }
            await self._render_schedule_preview(chat_id=chat_id, message_id=None)
        except Exception as exc:
            _PENDING_SCHEDULE_STATES.pop(chat_id, None)
            if msg_id:
                with contextlib.suppress(Exception):
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(msg_id)
                    )
            logger.error("finalize_scheduled_email_failed", error=str(exc))
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"✕ <b>Could not compose scheduled email:</b> {escape_html(str(exc))}",
            )

    async def _render_schedule_preview(
        self, chat_id: int, message_id: int | None = None
    ) -> None:
        """Render or update the pre-schedule preview card with Accept/Revise/Cancel/Tone."""
        import zoneinfo

        state = _PENDING_SCHEDULE_STATES.get(chat_id)
        if not state:
            return
        try:
            tz = zoneinfo.ZoneInfo(state.get("user_tz", "Asia/Kolkata"))
        except Exception:
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        local_time_str = state["scheduled_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")

        text = (
            "◈ <b>Scheduled Email Preview</b>\n\n"
            f"• <b>To:</b> <code>{escape_html(state['recipient'])}</code>\n"
            f"• <b>Send At:</b> <code>{escape_html(local_time_str)}</code>\n"
            f"• <b>Subject:</b> {escape_html(state['subject'])}\n\n"
            f"<blockquote>{escape_html(state['body'])}</blockquote>"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✓ Accept & Schedule", "callback_data": "sched_preview:accept"},
                    {"text": "✎ Revise", "callback_data": "sched_preview:revise"},
                    {"text": "✕ Cancel", "callback_data": "sched_preview:cancel"},
                ],
                [
                    {"text": "▪ Shorter", "callback_data": "sched_preview:tone:shorter"},
                    {"text": "▪ Formal", "callback_data": "sched_preview:tone:formal"},
                    {"text": "▪ Friendly", "callback_data": "sched_preview:tone:friendly"},
                    {"text": "▪ Direct", "callback_data": "sched_preview:tone:firm"},
                ],
            ]
        }
        if message_id:
            await self.telegram_client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
        else:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
            )

    async def _handle_schedule_preview_callback(
        self,
        chat_id: int,
        message_id: int | None,
        user_id: UUID | str,
        action: str,
    ) -> None:
        """Handle Accept, Revise, Cancel, or Tone buttons on a scheduled email preview."""
        import zoneinfo

        state = _PENDING_SCHEDULE_STATES.get(chat_id)
        if not state or state.get("stage") != "review":
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="› This preview has expired. Use /schedule to start a new one.",
            )
            return

        if action == "cancel":
            _PENDING_SCHEDULE_STATES.pop(chat_id, None)
            if message_id:
                await self.telegram_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="✕ <b>Scheduled Email Cancelled.</b>",
                    reply_markup=None,
                )
            else:
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✕ <b>Scheduled Email Cancelled.</b>"
                )
            return

        if action == "revise":
            state["awaiting_revision"] = True
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="✎ <b>Reply with your revision instructions</b> (or /cancel to abort):",
            )
            return

        if action.startswith("tone:"):
            tone_name = action.split(":", 1)[1]
            await self._revise_schedule_preview(
                chat_id=chat_id,
                user_id=user_id,
                tone=tone_name,
                message_id=message_id,
            )
            return

        if action == "accept":
            _PENDING_SCHEDULE_STATES.pop(chat_id, None)
            sched_svc = ScheduledEmailService(
                settings=self.settings, groq_service=self.groq_service
            )
            created = await sched_svc.create_scheduled_email(
                user_id=user_id,
                recipient=state["recipient"],
                subject=state["subject"],
                body=state["body"],
                scheduled_at=state["scheduled_at"],
            )
            try:
                tz = zoneinfo.ZoneInfo(state.get("user_tz", "Asia/Kolkata"))
            except Exception:
                tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            local_time_str = state["scheduled_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")

            confirm_text = (
                "✓ <b>Email Scheduled</b>\n\n"
                f"• <b>To:</b> <code>{escape_html(state['recipient'])}</code>\n"
                f"• <b>Send At:</b> <code>{escape_html(local_time_str)}</code>\n"
                f"• <b>Subject:</b> {escape_html(state['subject'])}\n\n"
                f"<blockquote>{escape_html(state['body'])}</blockquote>\n\n"
                "<i>Use /manage anytime to edit, reschedule, or cancel.</i>"
            )
            confirm_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "✎ Edit Content",
                            "callback_data": f"sched_edit:content:{created['id']}",
                        },
                        {
                            "text": "◷ Reschedule",
                            "callback_data": f"sched_edit:time:{created['id']}",
                        },
                        {
                            "text": "✕ Cancel",
                            "callback_data": f"cancel_sched:{created['id']}",
                        },
                    ]
                ]
            }
            if message_id:
                await self.telegram_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=confirm_text,
                    reply_markup=confirm_markup,
                )
            else:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=confirm_text,
                    reply_markup=confirm_markup,
                )

    async def _revise_schedule_preview(
        self,
        chat_id: int,
        user_id: UUID | str,
        instruction: str = "",
        tone: str | None = None,
        message_id: int | None = None,
    ) -> None:
        """Revise the in-flight scheduled email preview using Groq LLM."""
        state = _PENDING_SCHEDULE_STATES.get(chat_id)
        if not state:
            return
        user_groq_key: str | None = None
        if self.auth_service and hasattr(self.auth_service, "get_user_groq_api_key"):
            with contextlib.suppress(Exception):
                user_groq_key = await self.auth_service.get_user_groq_api_key(user_id)

        sched_svc = ScheduledEmailService(
            settings=self.settings, groq_service=self.groq_service
        )
        new_subj, new_body = await sched_svc.revise_subject_and_body(
            subject=state["subject"],
            body=state["body"],
            instruction=instruction,
            tone=tone,
            api_key=user_groq_key,
        )
        state["subject"] = new_subj
        state["body"] = new_body
        state["created_at"] = time.time()
        await self._render_schedule_preview(chat_id=chat_id, message_id=message_id)

    async def _apply_scheduled_edit(
        self, chat_id: int, edit_state: dict[str, Any], text: str
    ) -> None:
        """Apply post-schedule edit (content, recipient, or time) to an existing scheduled email."""
        import zoneinfo

        user_id = edit_state["user_id"]
        sched_id = edit_state["scheduled_id"]
        field = edit_state["field"]
        sched_svc = ScheduledEmailService(
            settings=self.settings, groq_service=self.groq_service
        )

        if field == "recipient":
            found_email = extract_email_address(text)
            if not found_email:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text="✕ <i>Please enter a valid email address, or /cancel to abort.</i>",
                )
                return
            _PENDING_SCHEDULE_EDIT_STATES.pop(chat_id, None)
            updated = await sched_svc.update_scheduled_email(
                user_id=user_id, scheduled_id=sched_id, recipient=found_email
            )
            msg = (
                f"✓ <b>Recipient updated</b> to <code>{escape_html(found_email)}</code>."
                if updated
                else "✕ Scheduled email was not found or already sent."
            )
            await self.telegram_client.send_message(chat_id=chat_id, text=msg)
            return

        if field == "time":
            user_obj = await self.user_repo.get_by_id(user_id)
            user_tz = (
                user_obj.timezone
                if user_obj and user_obj.timezone and user_obj.timezone != "UTC"
                else "Asia/Kolkata"
            )
            parsed_dt = parse_schedule_time(text, user_timezone=user_tz)
            if not parsed_dt:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "✕ <i>Could not understand that time or it is in the past.</i>\n"
                        "Try <code>in 45m</code>, <code>21:30</code>, "
                        "<code>tomorrow 09:00</code>, or /cancel."
                    ),
                )
                return
            _PENDING_SCHEDULE_EDIT_STATES.pop(chat_id, None)
            updated = await sched_svc.update_scheduled_email(
                user_id=user_id, scheduled_id=sched_id, scheduled_at=parsed_dt
            )
            try:
                tz = zoneinfo.ZoneInfo(user_tz)
            except Exception:
                tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            local_str = parsed_dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
            msg = (
                f"✓ <b>Rescheduled</b> to <code>{escape_html(local_str)}</code>."
                if updated
                else "✕ Scheduled email was not found or already sent."
            )
            await self.telegram_client.send_message(chat_id=chat_id, text=msg)
            return

        if field == "content":
            _PENDING_SCHEDULE_EDIT_STATES.pop(chat_id, None)
            existing = await sched_svc.get_scheduled_email(user_id=user_id, scheduled_id=sched_id)
            if not existing or existing.get("status") != "pending":
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✕ Scheduled email was not found or already sent."
                )
                return
            user_groq_key: str | None = None
            if self.auth_service and hasattr(self.auth_service, "get_user_groq_api_key"):
                with contextlib.suppress(Exception):
                    user_groq_key = await self.auth_service.get_user_groq_api_key(user_id)
            new_subj, new_body = await sched_svc.revise_subject_and_body(
                subject=existing["subject"],
                body=existing["body"],
                instruction=text,
                api_key=user_groq_key,
            )
            updated = await sched_svc.update_scheduled_email(
                user_id=user_id,
                scheduled_id=sched_id,
                subject=new_subj,
                body=new_body,
            )
            if not updated:
                await self.telegram_client.send_message(
                    chat_id=chat_id, text="✕ Scheduled email was not found or already sent."
                )
                return
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=(
                    "✓ <b>Scheduled Email Updated</b>\n\n"
                    f"• <b>Subject:</b> {escape_html(new_subj)}\n\n"
                    f"<blockquote>{escape_html(new_body)}</blockquote>"
                ),
            )

    async def _send_scheduled_list(self, chat_id: int, user_id: UUID | str) -> None:
        """Display pending scheduled emails with Edit Content, Recipient, Time, and Cancel."""
        import zoneinfo

        try:
            user = await self.user_repo.get_by_id(user_id)
            user_tz = (
                user.timezone
                if user and user.timezone and user.timezone != "UTC"
                else "Asia/Kolkata"
            )
            try:
                tz = zoneinfo.ZoneInfo(user_tz)
            except Exception:
                tz = zoneinfo.ZoneInfo("Asia/Kolkata")

            items = await ScheduledEmailService(settings=self.settings).list_pending_for_user(
                user_id=user_id, limit=5
            )
            if not items:
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "◷ <b>Manage Scheduled Emails</b>\n\n"
                        "› No pending scheduled emails.\n"
                        "<i>Use /schedule to schedule one.</i>"
                    ),
                )
                return

            for item in items:
                dt_raw = datetime.fromisoformat(str(item["scheduled_at"]).replace("Z", "+00:00"))
                dt_str = dt_raw.astimezone(tz).strftime("%d %b %H:%M %Z")
                card_text = (
                    f"◷ <b>{escape_html(item['subject'])}</b>\n"
                    f"• <b>To:</b> <code>{escape_html(item['recipient'])}</code>\n"
                    f"• <b>Send At:</b> <code>{escape_html(dt_str)}</code>\n\n"
                    f"<blockquote>{escape_html(item['body'][:400])}</blockquote>"
                )
                card_markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✎ Content",
                                "callback_data": f"sched_edit:content:{item['id']}",
                            },
                            {
                                "text": "✎ Recipient",
                                "callback_data": f"sched_edit:recipient:{item['id']}",
                            },
                        ],
                        [
                            {
                                "text": "◷ Reschedule",
                                "callback_data": f"sched_edit:time:{item['id']}",
                            },
                            {
                                "text": "✕ Cancel",
                                "callback_data": f"cancel_sched:{item['id']}",
                            },
                        ],
                    ]
                }
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=card_text,
                    reply_markup=card_markup,
                )
        except Exception as exc:
            logger.error("send_scheduled_list_failed", error=str(exc))
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=f"✕ <b>Could not load scheduled emails:</b> {escape_html(str(exc))}",
            )
