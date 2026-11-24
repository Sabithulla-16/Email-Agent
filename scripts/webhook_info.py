"""Manual fallback script to inspect Telegram webhook info."""

import asyncio
import sys

from app.core.config import get_settings
from app.services.telegram_client import TelegramClient


async def main() -> None:
    settings = get_settings()
    tg_client = TelegramClient(settings=settings)

    print("Fetching Telegram webhook info...")
    try:
        info = await tg_client.get_webhook_info()
        print("\n--- Telegram Webhook Info ---")
        if info:
            print(f"URL: {info.get('url')}")
            print(f"Has Custom Certificate: {info.get('has_custom_certificate')}")
            print(f"Pending Update Count: {info.get('pending_update_count')}")
            if info.get("last_error_date"):
                print(f"Last Error Date: {info.get('last_error_date')}")
            if info.get("last_error_message"):
                print(f"Last Error Message: {info.get('last_error_message')}")
            if info.get("max_connections"):
                print(f"Max Connections: {info.get('max_connections')}")
        else:
            print("No webhook currently configured.")
    except Exception as exc:
        print(f"Error fetching webhook info: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
