"""Telegram Bot application using python-telegram-bot HTTPXRequest (production architecture)."""

import asyncio
import contextlib
import html
import random
import socket
import ssl
import sys
from uuid import UUID

import structlog
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.core.config import get_settings
from app.db.client import close_supabase, get_supabase, init_supabase
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.telegram_client import TelegramClient

# Force IPv4 socket resolution on Windows to avoid dual-stack ISP packet drops
if sys.platform == "win32":
    _orig_getaddrinfo = socket.getaddrinfo

    def _v4_getaddrinfo(
        host: str, port: int | str, *_args: object, **_kwargs: object
    ) -> list[object]:
        return _orig_getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)  # type: ignore[arg-type]

    socket.getaddrinfo = _v4_getaddrinfo

logger = structlog.get_logger("telegram_bot")

# Per-chat revision context tracker: chat_id -> draft_id being revised
_active_revisions: dict[int, UUID] = {}

RepoTuple = tuple[UserRepo, TelegramRepo, EmailRepo, DraftRepo, ApprovalService, TelegramClient]


async def _get_repos() -> RepoTuple:
    sb = get_supabase()
    settings = get_settings()
    user_repo = UserRepo(sb)
    telegram_repo = TelegramRepo(sb)
    email_repo = EmailRepo(sb)
    draft_repo = DraftRepo(sb)
    auth_service = AuthService(user_repo=user_repo, settings=settings)
    approval_service = ApprovalService(
        draft_repo=draft_repo,
        email_repo=email_repo,
        user_repo=user_repo,
        auth_service=auth_service,
        settings=settings,
    )
    telegram_client = TelegramClient(telegram_repo=telegram_repo, settings=settings)
    return user_repo, telegram_repo, email_repo, draft_repo, approval_service, telegram_client


async def handle_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command - send Connect Gmail inline button or show status."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    user_repo, telegram_repo, _, draft_repo, _, _ = await _get_repos()
    settings = get_settings()

    # Check if this chat_id is already linked
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        backend_url = settings.PUBLIC_BASE_URL.rstrip("/")
        connect_url = f"{backend_url}/api/v1/auth/google?chat_id={chat_id}"
        keyboard = [[InlineKeyboardButton("⟡ Connect Gmail", url=connect_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "✦ <b>Welcome to Email Agent</b>\n\n"
            "Connect your Gmail account to review incoming emails, draft replies with Groq AI, "
            "and safely approve or revise drafts directly in Telegram:\n",
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return

    user = await user_repo.get_by_id(link.user_id)
    pending_drafts = await draft_repo.get_pending_approval_by_user(link.user_id)
    count = len(pending_drafts)

    await update.message.reply_text(
        f"✦ <b>Email Agent Active</b>\n\n"
        f"Connected: <code>{user.email if user else 'Unknown'}</code>\n"
        f"User: <b>{user.name if user and user.name else 'User'}</b>\n"
        f"Pending Drafts: <b>{count}</b>\n\n"
        "Commands:\n"
        "/pending — View & approve pending drafts\n"
        "/help — Show command reference",
        parse_mode="HTML",
    )


async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pending command - display all drafts awaiting approval with buttons."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    _, telegram_repo, email_repo, draft_repo, _, _ = await _get_repos()

    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        await handle_start(update, context)
        return

    pending_drafts = await draft_repo.get_pending_approval_by_user(link.user_id)
    if not pending_drafts:
        await update.message.reply_text(
            "— No pending drafts awaiting approval. Your inbox is up to date!"
        )
        return

    for draft in pending_drafts:
        email = await email_repo.get_by_id(draft.email_id)
        sender = email.sender_email if email else "Unknown"
        subject = draft.subject or (email.subject if email else "No Subject")

        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"appr_{draft.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_{draft.id}"),
                InlineKeyboardButton("✏️ Revise", callback_data=f"rev_{draft.id}"),
            ]
        ]
        text = (
            f"✉ <b>Draft Reply Awaiting Approval</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>To:</b> <code>{html.escape(sender)}</code>\n"
            f"<b>Subject:</b> {html.escape(subject)}\n\n"
            f"{html.escape(draft.draft_body)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )


async def handle_callback_query(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks: Approve, Reject, Revise."""
    query = update.callback_query
    if not query or not query.data or not update.effective_chat:
        return

    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    _, telegram_repo, _, _, approval_service, _ = await _get_repos()
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        await query.edit_message_text("Session expired. Please use /start to reconnect.")
        return

    if data.startswith("appr_"):
        draft_id = UUID(data[5:])
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            sent_email = await approval_service.approve_draft(
                draft_id=draft_id, user_id=link.user_id
            )
            orig_text = query.message.text_html if query.message else ""
            await query.edit_message_text(
                f"{orig_text}\n\n✅ <b>Draft Approved and Sent via Gmail!</b>\n"
                f"Message ID: <code>{sent_email.gmail_id}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("telegram_approve_failed", error=str(exc))
            await query.edit_message_text(f"Failed to send email: {exc}")

    elif data.startswith("rej_"):
        draft_id = UUID(data[4:])
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await approval_service.reject_draft(draft_id=draft_id, user_id=link.user_id)
            orig_text = query.message.text_html if query.message else ""
            await query.edit_message_text(
                f"{orig_text}\n\n❌ <b>Draft Rejected</b>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("telegram_reject_failed", error=str(exc))
            await query.edit_message_text(f"Failed to reject draft: {exc}")

    elif data.startswith("rev_"):
        draft_id = UUID(data[4:])
        _active_revisions[chat_id] = draft_id
        await query.message.reply_text(
            "✏️ <b>Draft Revision</b>\n\n"
            "Please reply with your instructions (e.g., 'Make it concise'):",
            parse_mode="HTML",
        )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages, including draft revision instructions."""
    if not update.effective_chat or not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text.startswith("/"):
        return

    _, telegram_repo, email_repo, _, approval_service, _ = await _get_repos()
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        await handle_start(update, context)
        return

    if chat_id in _active_revisions:
        draft_id = _active_revisions.pop(chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            revised_draft = await approval_service.revise_draft(
                draft_id=draft_id,
                user_id=link.user_id,
                instructions=text,
            )
            email = await email_repo.get_by_id(revised_draft.email_id)
            sender = email.sender_email if email else "Unknown"

            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"appr_{revised_draft.id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"rej_{revised_draft.id}"),
                    InlineKeyboardButton("✏️ Revise", callback_data=f"rev_{revised_draft.id}"),
                ]
            ]
            msg = (
                f"✉ <b>Revised Draft Reply</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>To:</b> <code>{html.escape(sender)}</code>\n"
                f"<b>Subject:</b> {html.escape(revised_draft.subject)}\n\n"
                f"{html.escape(revised_draft.draft_body)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            await update.message.reply_text(
                msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception as exc:
            logger.error("revision_failed", error=str(exc))
            await update.message.reply_text(f"Revision error: {exc}")
        return

    await update.message.reply_text(
        "I received your message. Use /pending to see drafts, or /help for commands."
    )


async def handle_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not update.message:
        return
    help_text = (
        "✦ <b>Email Agent — Command Reference</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Commands:</b>\n"
        "/start — Welcome & account connection\n"
        "/pending — View all drafts awaiting your approval\n"
        "/help — Show this help menu\n\n"
        "<b>Safety Invariant:</b>\n"
        "Emails are NEVER sent automatically. "
        "Every reply requires your explicit approval via the [✅ Approve] button."
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def handle_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setkey command - securely store user-provided Groq API key."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    user_repo, telegram_repo, _, _, _, _ = await _get_repos()
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        await update.message.reply_text("⚠️ Please connect your Gmail first using /start.")
        return

    # Delete the message containing the raw API key for privacy
    try:
        await update.message.delete()
    except Exception:
        pass

    args = context.args or []
    if not args:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "ℹ️ <b>How to set your Groq API key:</b>\n\n"
                "Send: <code>/setkey gsk_your_groq_key_here</code>\n\n"
                "<i>(Get a free key in 30 seconds at https://console.groq.com/keys)</i>\n"
                "<i>Your key is encrypted with AES-256 and your message is deleted immediately for privacy.</i>"
            ),
            parse_mode="HTML",
        )
        return

    raw_key = args[0].strip()
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🔄 <i>Validating and encrypting your Groq API key...</i>",
        parse_mode="HTML",
    )
    auth_svc = AuthService(user_repo=user_repo, settings=get_settings())
    try:
        masked = await auth_svc.set_user_groq_api_key(link.user_id, raw_key)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ <b>Groq API Key Configured Successfully!</b>\n\n"
                f"• Key: <code>{masked}</code>\n"
                f"• Encryption: <b>AES-256-GCM</b>\n\n"
                "All email summaries, triage analysis, and reply drafts will now run on your dedicated Groq quota.\n"
                "Use /mykey to view status or /removekey to delete it."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Failed to set Groq key:</b> {exc}",
            parse_mode="HTML",
        )


async def handle_mykey(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mykey command - view current Groq API key status."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    user_repo, telegram_repo, _, _, _, _ = await _get_repos()
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        await update.message.reply_text("⚠️ Please connect your Gmail first using /start.")
        return

    auth_svc = AuthService(user_repo=user_repo, settings=get_settings())
    status = await auth_svc.get_user_groq_key_status(link.user_id)
    if status.get("has_key"):
        masked = status.get("masked_key", "Configured")
        await update.message.reply_text(
            f"🔑 <b>Your Groq API Key:</b>\n\n"
            f"• Status: <b>Active</b>\n"
            f"• Key: <code>{masked}</code>\n\n"
            f"To update it, send: <code>/setkey gsk_...</code>\n"
            f"To remove it, send: /removekey",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "ℹ️ <b>No Personal Groq API Key Configured</b>\n\n"
            "Currently using default system tier.\n\n"
            "To connect your own dedicated Groq quota, send:\n"
            "<code>/setkey gsk_your_key_here</code>\n\n"
            "<i>(Get a free key at https://console.groq.com/keys)</i>",
            parse_mode="HTML",
        )


async def handle_removekey(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /removekey command - delete stored Groq API key."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    user_repo, telegram_repo, _, _, _, _ = await _get_repos()
    link = await telegram_repo.get_link_by_chat_id(chat_id)
    if not link:
        return

    auth_svc = AuthService(user_repo=user_repo, settings=get_settings())
    await auth_svc.remove_user_groq_api_key(link.user_id)
    await update.message.reply_text(
        "🗑️ <b>Personal Groq API Key Removed.</b> Reverted to system default.",
        parse_mode="HTML",
    )


def build_application() -> Application:
    """Build python-telegram-bot Application instance with configured HTTPXRequest."""
    settings = get_settings()
    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()

    # TLS legacy connect context to handle server session renegotiation smoothly
    ssl_ctx = ssl.create_default_context()
    with contextlib.suppress(AttributeError):
        ssl_ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT

    request = HTTPXRequest(
        connection_pool_size=16,
        http_version="1.1",
        connect_timeout=15.0,
        read_timeout=25.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        httpx_kwargs={
            "verify": ssl_ctx,
            "headers": {"Connection": "close"},
        },
    )

    app = ApplicationBuilder().token(token).request(request).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("pending", handle_pending))
    app.add_handler(CommandHandler("setkey", handle_setkey))
    app.add_handler(CommandHandler("mykey", handle_mykey))
    app.add_handler(CommandHandler("removekey", handle_removekey))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    return app


async def run_bot_async() -> None:
    """Asynchronous entrypoint for bot bootstrap and polling with exponential backoff."""
    settings = get_settings()
    await init_supabase(settings)

    app = build_application()

    print("=======================================================", flush=True)
    print("  Telegram Bot is starting (python-telegram-bot)...", flush=True)
    print(f"  Mode: {settings.TELEGRAM_MODE.upper()}", flush=True)
    print("  Commands: /start, /pending, /help", flush=True)
    print("  Press Ctrl+C to stop.", flush=True)
    print("=======================================================\n", flush=True)

    max_bootstrap_retries = 10
    base_delay = 2.0
    max_delay = 30.0

    for attempt in range(max_bootstrap_retries):
        try:
            await app.initialize()
            me = await app.bot.get_me()

            # Log getMe results cleanly without exposing bot token
            logger.info(
                "telegram_bot_initialized",
                bot_id=me.id,
                username=me.username,
                first_name=me.first_name,
                can_join_groups=me.can_join_groups,
            )

            # In polling mode: check webhook info, delete webhook, handle 409 Conflict
            webhook_info = await app.bot.get_webhook_info()
            logger.info(
                "telegram_webhook_info",
                url=webhook_info.url,
                has_custom_certificate=webhook_info.has_custom_certificate,
                pending_update_count=webhook_info.pending_update_count,
                last_error_date=webhook_info.last_error_date,
                last_error_message=webhook_info.last_error_message,
            )

            if settings.TELEGRAM_MODE == "polling":
                await app.bot.delete_webhook(drop_pending_updates=False)
                logger.info("telegram_webhook_deleted_for_polling")
            else:
                logger.info("telegram_webhook_preserved_for_webhook_mode")
            break

        except telegram.error.Conflict:
            msg = "Another instance of the Telegram bot is already running or holding the lock."
            logger.critical("telegram_409_conflict", message=msg)
            print(f"\n[CRITICAL 409 Conflict] {msg}\n", flush=True)
            return

        except telegram.error.RetryAfter as exc:
            wait_time = float(exc.retry_after) + 1.0
            logger.warning("telegram_rate_limited_429", retry_after=wait_time, attempt=attempt)
            print(
                f"Telegram 429 flood control: waiting {wait_time:.1f}s "
                f"(retry {attempt + 1}/{max_bootstrap_retries})...",
                flush=True,
            )
            await asyncio.sleep(wait_time)

        except (telegram.error.TimedOut, telegram.error.NetworkError) as exc:
            jitter = random.uniform(0.5, 1.5)
            backoff = min(max_delay, (base_delay * (2**attempt)) + jitter)
            logger.warning(
                "telegram_bootstrap_network_error",
                error=str(exc),
                backoff=round(backoff, 2),
                attempt=attempt,
            )
            print(
                f"Telegram network delay ({exc}): retrying in {backoff:.1f}s "
                f"(retry {attempt + 1}/{max_bootstrap_retries})...",
                flush=True,
            )
            await asyncio.sleep(backoff)

    await app.start()
    if app.updater:
        await app.updater.start_polling(drop_pending_updates=False)

    logger.info("telegram_bot_polling_active", bot_username=me.username)
    print(f"\nTelegram bot @{me.username} is ACTIVE and polling for messages!\n", flush=True)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        await close_supabase()
        print("Telegram bot shutdown cleanly.")


def main() -> None:
    """Start the Telegram bot runner."""
    try:
        asyncio.run(run_bot_async())
    except (KeyboardInterrupt, SystemExit):
        print("\nTelegram bot stopped.")


if __name__ == "__main__":
    main()
