import sys
import os

# Add the root directory to the Python path so we can import 'src'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.client import supabase_client
from src.core.logging import logger

def test_connection():
    logger.info("🚀 Starting Supabase connection test...")
    
    try:
        # Let's do a simple query to check if the 'users' table exists
        response = supabase_client.table('users').select('id').limit(1).execute()
        
        logger.info(f"✅ Connection successful! Database query returned: {response.data}")
        logger.info("🎉 Your backend is now officially connected to Supabase!")
        
    except Exception as e:
        logger.error(f"❌ Connection test failed: {e}")

if __name__ == "__main__":
    test_connection()