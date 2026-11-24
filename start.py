"""Application startup script running FastAPI and Telegram bot according to TELEGRAM_MODE."""

import os
import sys
import threading

import uvicorn

from app.core.config import get_settings
from telegram_bot import main as run_telegram_bot


def start_bot_thread() -> None:
    """Run Telegram Bot polling in a background thread."""
    try:
        run_telegram_bot()
    except Exception as exc:
        print(f"Telegram Bot error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    settings = get_settings()
    port = int(os.environ.get("PORT", settings.PORT))
    host = os.environ.get("HOST", settings.HOST)

    if settings.TELEGRAM_MODE == "polling":
        tg_thread = threading.Thread(target=start_bot_thread, daemon=True)
        tg_thread.start()
        print("Telegram Bot thread started in polling mode.", flush=True)
    else:
        print(
            "Telegram Bot running in webhook mode (updates handled via /api/v1/telegram/webhook).",
            flush=True,
        )

    print(f"Starting FastAPI server on http://{host}:{port}...", flush=True)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
    )
