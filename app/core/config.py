"""Application settings and configuration management via pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for Email Agent backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Environment & Server
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    APP_SECRET_KEY: SecretStr = Field(
        default=SecretStr("insecure-dev-app-secret-key-at-least-32-chars-long"),
        description="Key used for cryptographic signing of session cookies and CSRF tokens",
    )

    # Token Vault Encryption (AES-256-GCM requires 32-byte key)
    TOKEN_ENCRYPTION_KEY: SecretStr = Field(
        default=SecretStr("01234567890123456789012345678901"),  # 32 chars for local dev
        description="Primary 32-byte key for AES-256-GCM token vault",
    )
    TOKEN_KEY_VERSION: int = 1
    # Secondary keys for rotation support: JSON mapping version -> secret
    TOKEN_SECONDARY_KEYS: dict[int, SecretStr] = Field(default_factory=dict)

    # Supabase (Server-side Secret Key ONLY - never exposed to clients)
    SUPABASE_URL: str = Field(
        default="http://localhost:54321",
        description="Supabase project URL",
    )
    SUPABASE_SECRET_KEY: SecretStr = Field(
        default=SecretStr("dummy_supabase_secret_key_for_testing"),
        description="Supabase Service Role / Secret Key (bypasses RLS)",
    )

    # Redis & ARQ Queue
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for ARQ and caching",
    )

    # Groq LLM
    GROQ_API_KEY: SecretStr = Field(
        default=SecretStr("mock_groq_api_key"),
        description="Groq Cloud API Key",
    )
    GROQ_FAST_MODEL: str = Field(
        default="llama-3.1-8b-instant",
        description="Model for fast classification, summaries, and action extraction",
    )
    GROQ_DRAFT_MODEL: str = Field(
        default="llama-3.3-70b-versatile",
        description="Model for draft generation and revision loops",
    )
    GROQ_WHISPER_MODEL: str = Field(
        default="whisper-large-v3-turbo",
        description="Model for transcribing Telegram voice notes",
    )
    GROQ_MAX_RETRIES: int = 2
    GROQ_TIMEOUT_SECONDS: float = 30.0

    # Google OAuth 2.0 & Gmail
    GOOGLE_CLIENT_ID: str = Field(
        default="mock-google-client-id.apps.googleusercontent.com",
        description="Google OAuth 2.0 Client ID",
    )
    GOOGLE_CLIENT_SECRET: SecretStr = Field(
        default=SecretStr("mock-google-client-secret"),
        description="Google OAuth 2.0 Client Secret",
    )
    GOOGLE_REDIRECT_URI: str = Field(
        default="http://localhost:8000/api/v1/auth/google/callback",
        description="Google OAuth 2.0 redirect callback URL",
    )

    # Public Service Base URL (Render / Production)
    PUBLIC_BASE_URL: str = Field(
        default="http://localhost:8000",
        description="Public base URL for webhooks and OAuth redirects (e.g. Render service URL)",
    )

    # Telegram Bot
    TELEGRAM_MODE: Literal["polling", "webhook"] = Field(
        default="polling",
        description="Telegram bot operational mode: 'polling' for local dev, 'webhook' for Render",
    )
    TELEGRAM_BOT_TOKEN: SecretStr = Field(
        default=SecretStr("123456:mock_telegram_bot_token"),
        description="Telegram Bot API token from BotFather",
    )
    TELEGRAM_WEBHOOK_SECRET: SecretStr = Field(
        default=SecretStr("mock_telegram_webhook_secret_header_token"),
        description="Secret token passed in X-Telegram-Bot-Api-Secret-Token",
    )
    TELEGRAM_BOT_USERNAME: str = Field(
        default="EmailAgentBot",
        description="Username of Telegram bot for deep links",
    )

    # Client Web App & CORS
    FRONTEND_BASE_URL: str = "http://localhost:3000"
    CORS_ORIGINS: list[str] | str = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Email Sync & Processing Configuration
    SYNC_LOOKBACK_DAYS: int = 3
    SYNC_LOCK_TTL_SECONDS: int = 300
    DRAFT_EXPIRY_HOURS: int = 48
    EMBEDDING_DIMENSION: int = 384
    EMAIL_BODY_MAX_CHARS: int = 15000

    # Sentry Error Tracking (Optional)
    SENTRY_DSN: SecretStr | None = None

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return v
        return []

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    return Settings()
