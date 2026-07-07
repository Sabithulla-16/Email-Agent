from fastapi import APIRouter, HTTPException
from src.tools.google_auth import exchange_code_for_tokens
from src.db.client import supabase_client
from src.core.logging import logger
import uuid
import httpx

router = APIRouter()

@router.get("/auth/callback")
async def auth_callback(code: str, state: str):
    """
    Handles the redirect from Google OAuth.
    The 'state' parameter contains the Telegram ID of the user.
    """
    telegram_id = state
    logger.info(f"✅ Received OAuth callback for Telegram ID: {telegram_id}")

    # 1. Check if user exists in our public.users table
    response = supabase_client.table('users').select('id').eq('telegram_id', int(telegram_id)).execute()

    if not response.data:
        # If they don't exist, create a new record for them
        new_uuid = str(uuid.uuid4())
        supabase_client.table('users').insert({
            'id': new_uuid,
            'telegram_id': int(telegram_id)
        }).execute()
        user_uuid = new_uuid
        logger.info(f"Created new user record for Telegram ID: {telegram_id}")
    else:
        user_uuid = response.data[0]['id']

    # 2. Exchange the code for tokens and save to Supabase
    success = exchange_code_for_tokens(code, user_uuid)

    if success:
        return {
            "message": "✅ Google account connected successfully! You can close this tab and go back to Telegram."
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to connect account.")
