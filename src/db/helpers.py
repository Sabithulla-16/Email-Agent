from src.db.client import supabase_client
from src.core.logging import logger

def get_user_name(user_uuid: str) -> str:
    """Fetches the user's full name from the database."""
    try:
        response = supabase_client.table('users').select('full_name').eq('id', user_uuid).execute()
        if response.data and response.data[0].get('full_name'):
            return response.data[0]['full_name']
    except Exception as e:
        logger.error(f"Error fetching user name: {e}")
    
    # Fallback name
    return "Valtry"

def get_user_telegram_id(user_uuid: str) -> int | None:
    """Fetches the user's Telegram ID from the database."""
    try:
        response = supabase_client.table('users').select('telegram_id').eq('id', user_uuid).execute()
        if response.data and response.data[0].get('telegram_id'):
            return response.data[0]['telegram_id']
    except Exception as e:
        logger.error(f"Error fetching Telegram ID: {e}")
    
    return None