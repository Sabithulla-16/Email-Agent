from supabase import create_client, Client
from src.core.config import settings
from src.core.logging import logger

def get_supabase_client() -> Client:
    """
    Initializes and returns the Supabase client.
    We use the service_role key here because this is a backend application 
    and needs to bypass Row Level Security (RLS).
    """
    try:
        supabase: Client = create_client(
            settings.SUPABASE_URL, 
            settings.SUPABASE_SERVICE_ROLE_KEY
        )
        logger.info("✅ Successfully connected to Supabase.")
        return supabase
    except Exception as e:
        logger.error(f"❌ Failed to connect to Supabase: {e}")
        raise

def get_user_uuid_by_telegram(telegram_id: int) -> str | None:
    """Fetches the Supabase UUID for a given Telegram ID."""
    try:
        response = supabase_client.table('users').select('id').eq('telegram_id', telegram_id).execute()
        if response.data:
            return response.data[0]['id']
        return None
    except Exception as e:
        logger.error(f"Error fetching user UUID: {e}")
        return None

# Initialize the client globally so it can be imported and reused
supabase_client = get_supabase_client()