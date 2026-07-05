from supabase import Client
from src.db.client import supabase_client

def get_db() -> Client:
    """
    FastAPI dependency to get the Supabase client.
    Usage in FastAPI: db: Client = Depends(get_db)
    """
    return supabase_client