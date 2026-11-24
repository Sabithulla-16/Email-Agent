"""Manual fallback script to register Telegram webhook."""

import asyncio
import sys

from app.core.config import get_settings
from app.services.telegram_client import TelegramClient


async def main() -> None:
    settings = get_settings()
    tg_client = TelegramClient(settings=settings)

    webhook_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/telegram/webhook"
    secret_token = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()

    print(f"Setting Telegram webhook to: {webhook_url}...")
    try:
        success = await tg_client.set_webhook(url=webhook_url, secret_token=secret_token)
        if success:
            print("Successfully requested webhook configuration.")
        else:
            print("Failed to configure webhook. Check token and URL.")

        info = await tg_client.get_webhook_info()
        print("\n--- Current Webhook Info ---")
        if info:
            print(f"URL: {info.get('url')}")
            print(f"Has Custom Certificate: {info.get('has_custom_certificate')}")
            print(f"Pending Update Count: {info.get('pending_update_count')}")
            if info.get("last_error_date"):
                print(f"Last Error Date: {info.get('last_error_date')}")
            if info.get("last_error_message"):
                print(f"Last Error Message: {info.get('last_error_message')}")
        else:
            print("No webhook info returned.")
    except Exception as exc:
        print(f"Error setting webhook: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
