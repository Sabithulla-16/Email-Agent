"""Telegram Bot integration runner using the official python-telegram-bot package."""

import asyncio
import socket

import structlog
import telegram.error
from telegram import Update
from telegram.ext import (
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
from app.services.notification_service import NotificationService
from app.services.telegram_client import TelegramClient
from app.services.telegram_service import TelegramService

# Force IPv4 socket resolution to prevent Windows dual-stack IPv6 ISP packet drops
orig_getaddrinfo = socket.getaddrinfo


def getaddrinfo_v4(host: str, port: int | str, _family: int = 0, *args: object) -> list[object]:
    return orig_getaddrinfo(host, port, socket.AF_INET, *args)  # type: ignore[arg-type]


socket.getaddrinfo = getaddrinfo_v4

logger = structlog.get_logger("scripts.telegram_bot")


async def build_telegram_service() -> tuple[TelegramService, TelegramClient]:
    settings = get_settings()
    await init_supabase(settings)

    user_repo = UserRepo(get_supabase())
    telegram_repo = TelegramRepo(get_supabase())
    email_repo = EmailRepo(get_supabase())
    draft_repo = DraftRepo(get_supabase())
    auth_svc = AuthService(user_repo=user_repo, settings=settings)
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
        settings=settings,
    )
    tg_client = TelegramClient(telegram_repo=telegram_repo, settings=settings)
    tg_service = TelegramService(
        telegram_repo=telegram_repo,
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        approval_service=approval_svc,
        notification_service=notif_svc,
        telegram_client=tg_client,
        settings=settings,
    )
    return tg_service, tg_client


async def async_main() -> None:
    settings = get_settings()
    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()

    tg_service, tg_client = await build_telegram_service()

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        httpx_kwargs={"verify": tg_client._ssl_ctx},
    )

    application = ApplicationBuilder().token(token).request(request).build()

    async def update_dispatcher(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update or not tg_service:
            return
        payload = update.to_dict()
        try:
            await tg_service.process_webhook_update(payload)
        except Exception as exc:
            logger.error("telegram_update_dispatch_error", error=str(exc))

    application.add_handler(
        CommandHandler(["start", "pending", "urgent", "quiet", "help"], update_dispatcher)
    )
    application.add_handler(CallbackQueryHandler(update_dispatcher))
    application.add_handler(
        MessageHandler(filters.VOICE | (filters.TEXT & ~filters.COMMAND), update_dispatcher)
    )

    print("Connecting to Telegram Bot API with python-telegram-bot...", flush=True)

    while True:
        try:
            await application.initialize()
            bot_info = await application.bot.get_me()
            await application.bot.delete_webhook(drop_pending_updates=False)
            break
        except telegram.error.RetryAfter as exc:
            print(
                f"Telegram cooldown active: waiting {exc.retry_after}s before starting...",
                flush=True,
            )
            await asyncio.sleep(exc.retry_after + 1)

    print("\n=======================================================", flush=True)
    print(f"  Telegram Bot @{bot_info.username} is ACTIVE (python-telegram-bot)!", flush=True)
    print(f"  Open Telegram and chat with: @{bot_info.username}", flush=True)
    print("  Commands: /start <token>, /pending, /urgent, /quiet", flush=True)
    print("  Press Ctrl+C to stop", flush=True)
    print("=======================================================\n", flush=True)

    await application.start()
    if application.updater:
        await application.updater.start_polling(drop_pending_updates=False)

    print(f"Bot @{bot_info.username} is now polling for messages...\n", flush=True)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        if application.updater:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await close_supabase()


def main() -> None:
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped cleanly.")


if __name__ == "__main__":
    main()
