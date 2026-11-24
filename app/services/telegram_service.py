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
        settings: Settings | None = None,
    ) -> None:
        self.telegram_repo = telegram_repo
        self.user_repo = user_repo
        self.email_repo = email_repo
        self.draft_repo = draft_repo
        self.approval_service = approval_service
        self.notification_service = notification_service
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
        await self.telegram_client.send_message(
            chat_id=chat_id,
            text="🔍 <i>Searching your emails...</i>",
        )
        try:
            qa_svc = InboxQAService(email_repo=self.email_repo, settings=self.settings)
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

            await self.telegram_client.send_message(
                chat_id=chat_id,
                text=resp_text,
            )
        except Exception as exc:
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

        # Handle /start with or without token
        if cmd == "/start":
            if len(parts) > 1:
                token_param = parts[1].strip()
                token_hash = hashlib.sha256(token_param.encode("utf-8")).hexdigest()
                user_id = await self.telegram_repo.consume_link_token(token_hash)
                if user_id:
                    await self.telegram_repo.link_chat(user_id=user_id, chat_id=chat_id)
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
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "👋 <b>Welcome to Email Agent!</b>\n\n"
                        "I am your personal AI email assistant. I summarize unread emails, "
                        "draft replies in your voice, and send only with your approval.\n\n"
                        "To get started, please connect your Gmail account:"
                    ),
                    reply_markup=reply_markup,
                )
            return

        # Authenticate existing linked user for subsequent commands
        link = await self.telegram_repo.get_link_by_chat_id(chat_id)
        if not link:
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="⚠️ Your Telegram account is not linked. Please connect via the web app.",
            )
            return

        user_id = link.user_id

        if cmd == "/disconnect":
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
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="🔄 <i>Synchronizing your inbox from Gmail...</i>",
            )
            try:
                cycle_res = await run_direct_sync_cycle()
                claimed = cycle_res.get("claimed_users", 0)
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Inbox Sync Complete!</b>\n"
                        f"Processed active users ({claimed}) and drafted replies."
                    ),
                )
            except Exception as exc:
                logger.error("telegram_sync_command_failed", error=str(exc))
                await self.telegram_client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Sync failed: {escape_html(str(exc))}",
                )

        elif cmd == "/digest":
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="📊 <i>Compiling your daily email digest...</i>",
            )
            try:
                digest_svc = DigestService(
                    user_repo=self.user_repo,
                    email_repo=self.email_repo,
                    draft_repo=self.draft_repo,
                    telegram_repo=self.telegram_repo,
                    settings=self.settings,
                )
                res = await digest_svc.generate_and_dispatch_digest(user_id)
                if res.get("status") == "skipped":
                    await self.telegram_client.send_message(
                        chat_id=chat_id,
                        text=f"ℹ️ Digest skipped: {res.get('reason', 'no recent activity')}",
                    )
            except Exception as exc:
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
                    "<b>Settings:</b>\n"
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
        transcript = await self.groq_service.transcribe_audio(audio_bytes, filename="voice.ogg")

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
