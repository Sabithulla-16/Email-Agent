from fastapi import APIRouter, Request
from telegram import Update
from src.bot.bot_instance import application
from src.core.logging import logger

router = APIRouter()

@router.post("/telegram")
async def telegram_webhook(request: Request):
    """Receives updates from Telegram."""
    try:
        data = await request.json()
        
        # FIX: Convert the raw JSON dict into a Telegram Update object
        update = Update.de_json(data, application.bot)
        
        # Now process the properly formatted Update object
        await application.process_update(update)
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")
        return {"status": "error", "message": str(e)}