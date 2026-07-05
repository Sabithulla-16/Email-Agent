from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Telegram
    TELEGRAM_BOT_TOKEN: str
    
    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERICE_ROLE_KEY: str
    
    # Groq
    GROQ_API_KEY: str
    
    # Google OAuth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str # We will set this directly in Render env vars
    
    # Gmail Pub/Sub
    GMAIL_PUBSUB_TOPIC: str

    class Config:
        env_file = ".env"

settings = Settings()