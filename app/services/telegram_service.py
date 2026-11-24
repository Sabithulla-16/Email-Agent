"""Telegram bot service orchestrating account linking, commands, callbacks, and voice revisions."""

import hashlib
import hmac
import secrets
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
from app.models.db import DraftStatus
from app.queue.redis import get_redis
from app.services.approval_service import ApprovalService
from app.services.digest_service import DigestService
from app.services.followup_service import FollowupService
from app.services.groq_service import GroqService
from app.services.inbox_qa import InboxQAService
from app.services.notification_service import NotificationService
from app.services.scheduler_runner import run_direct_sync_cycle
from app.services.telegram_client import TelegramClient, escape_html

logger = structlog.get_logger("app.services.telegram")

_START_MESSAGE_IDS: dict[int, int] = {}


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
                try:
                    await self.telegram_client.delete_message(
                        chat_id=chat_id, message_id=int(message["message_id"])
                    )
                except Exception:
                    pass
            await self._handle_command(chat_id, text)
            return

        # 3. Pending Interaction Processing (e.g. revision text reply)
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
                                "To enable AI email analysis and reply drafts on your own quota, enter your Groq API key:\n\n"
                                "<code>/setkey gsk_your_key_here</code>\n\n"
                                "<i>(Get a free key at https://console.groq.com/keys)</i>\n"
                                "<i>Your key is encrypted with AES-256 and your message is deleted immediately for privacy.</i>"
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
                        "<i>Your key is encrypted with AES-256 and your message is automatically deleted for privacy.</i>"
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
                        "All email summaries, triage analysis, and reply drafts will now run on your dedicated Groq quota.\n"
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
                    settings=self.settings,
                )
                res = await digest_svc.generate_and_dispatch_digest(user_id)
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

        elif cmd == "/help":
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=(
                    "🤖 <b>Email Agent Bot Commands:</b>\n\n"
                    "<b>Email & Approvals:</b>\n"
                    "• /pending - Show drafts waiting for approval\n"
                    "• /urgent - Show high priority emails\n"
                    "• /sync - Manually trigger Gmail sync\n"
                    "• /digest - Compile and send daily summary now\n\n"
                    "<b>Productivity & AI:</b>\n"
                    "• /ask &lt;query&gt; - Ask questions about your emails\n"
                    "• /followups - View tracked promises & commitments\n"
                    "• <i>Tip: Send any question in chat to search your emails!</i>\n"
                    "• <i>Tip: Send a voice note to revise any draft!</i>\n\n"
                    "<b>Settings & Keys:</b>\n"
                    "• /setkey &lt;key&gt; - Set personal Groq API key\n"
                    "• /mykey - Check current Groq API key status\n"
                    "• /removekey - Remove personal Groq key\n"
                    "• /snooze &lt;hours&gt; - Pause notifications\n"
                    "• /mute - Mute notifications for 24h\n"
                    "• /settings - View notification settings\n"
                    "• /disconnect - Unlink this Telegram chat"
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
