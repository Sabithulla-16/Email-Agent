"""Helper script to configure Telegram webhook and generate an account linking link."""

import asyncio
import sys

import httpx

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


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/setup_telegram_webhook.py <PUBLIC_HTTPS_URL>")
        print(
            "Example: uv run python scripts/setup_telegram_webhook.py https://cool-tiger-42.loca.lt"
        )
        sys.exit(1)

    public_url = sys.argv[1].rstrip("/")
    webhook_url = f"{public_url}/api/v1/telegram/webhook"

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

    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
    secret = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
    base_url = f"https://api.telegram.org/bot{token}"
    ssl_ctx = tg_client._ssl_ctx

    print("\n[1/3] Registering webhook with Telegram...")
    print(f"      Target: {webhook_url}")

    async with httpx.AsyncClient(verify=ssl_ctx, timeout=15.0) as client:
        r = await client.post(
            f"{base_url}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        print("      Result:", r.json())

    # Generate fresh linking deep link
    user = await user_repo.get_by_id("30715ef7-85bd-4286-955b-b1e71a3b542c")
    if not user:
        print("Error: Default user not found in database.")
        sys.exit(1)

    deep_link = await tg_service.create_link_deep_link(user.id)

    print("\n[2/3] Webhook configured successfully!")
    print("\n[3/3] Click this link on your phone/desktop Telegram to connect:")
    print(f"      👉 {deep_link}\n")

    await close_supabase()


if __name__ == "__main__":
    asyncio.run(main())
