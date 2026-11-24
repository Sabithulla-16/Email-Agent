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

    # Free-tier In-Process Scheduler (runs inside Web Service without paid worker)
    ENABLE_IN_PROCESS_SCHEDULER: bool = Field(
        default=True,
        description="Run periodic inbox sync inside the web service process",
    )
    IN_PROCESS_SYNC_INTERVAL_SECONDS: int = Field(
        default=300,
        description="Interval in seconds between periodic background sync cycles",
    )

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


def validate_startup_config(settings: Settings) -> None:
    """Validate all required environment variables for deployment.

    Fails fast with a detailed error message if any required environment variable
    is missing or insecure in staging or production environments.
    """
    if not (settings.is_production or settings.ENVIRONMENT == "staging"):
        return

    errors: list[str] = []

    # 1. APP_SECRET_KEY
    app_sec = settings.APP_SECRET_KEY.get_secret_value() if settings.APP_SECRET_KEY else ""
    if not app_sec or len(app_sec) < 32 or "insecure" in app_sec.lower():
        errors.append(
            "APP_SECRET_KEY: Must be a secure random string of at least 32 characters."
        )

    # 2. TOKEN_ENCRYPTION_KEY
    tok_sec = (
        settings.TOKEN_ENCRYPTION_KEY.get_secret_value()
        if settings.TOKEN_ENCRYPTION_KEY
        else ""
    )
    if not tok_sec or len(tok_sec.encode("utf-8")) != 32:
        errors.append(
            "TOKEN_ENCRYPTION_KEY: Must be exactly 32 bytes (256 bits) for AES-256-GCM vault."
        )

    # 3. SUPABASE_URL & SUPABASE_SECRET_KEY
    if (
        not settings.SUPABASE_URL
        or not settings.SUPABASE_URL.startswith("https://")
        or "localhost" in settings.SUPABASE_URL
    ):
        errors.append(
            "SUPABASE_URL: Must be a valid HTTPS URL (e.g. https://<project-ref>.supabase.co)."
        )
    sup_key = (
        settings.SUPABASE_SECRET_KEY.get_secret_value()
        if settings.SUPABASE_SECRET_KEY
        else ""
    )
    if not sup_key or "dummy" in sup_key.lower():
        errors.append(
            "SUPABASE_SECRET_KEY: Required and cannot be a dummy placeholder."
        )

    # 4. REDIS_URL
    if not settings.REDIS_URL or not (
        settings.REDIS_URL.startswith("redis://")
        or settings.REDIS_URL.startswith("rediss://")
    ):
        errors.append(
            "REDIS_URL: Must be a valid Redis connection string (redis:// or rediss://)."
        )

    # 5. GROQ_API_KEY
    groq_k = settings.GROQ_API_KEY.get_secret_value() if settings.GROQ_API_KEY else ""
    if not groq_k or "mock" in groq_k.lower():
        errors.append("GROQ_API_KEY: Required and cannot be a mock placeholder.")

    # 6. GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
    if not settings.GOOGLE_CLIENT_ID or "mock" in settings.GOOGLE_CLIENT_ID.lower():
        errors.append("GOOGLE_CLIENT_ID: Required and cannot be a mock placeholder.")
    gsec = (
        settings.GOOGLE_CLIENT_SECRET.get_secret_value()
        if settings.GOOGLE_CLIENT_SECRET
        else ""
    )
    if not gsec or "mock" in gsec.lower():
        errors.append("GOOGLE_CLIENT_SECRET: Required and cannot be a mock placeholder.")
    if not settings.GOOGLE_REDIRECT_URI or not settings.GOOGLE_REDIRECT_URI.startswith("https://"):
        errors.append(
            "GOOGLE_REDIRECT_URI: Must be a public HTTPS callback URL "
            "(e.g. https://<service>.onrender.com/api/v1/auth/google/callback)."
        )

    # 7. PUBLIC_BASE_URL
    if not settings.PUBLIC_BASE_URL or not settings.PUBLIC_BASE_URL.startswith("https://"):
        errors.append(
            "PUBLIC_BASE_URL: Must be a public HTTPS URL (e.g. https://<service>.onrender.com)."
        )

    # 8. TELEGRAM_BOT_TOKEN & TELEGRAM_WEBHOOK_SECRET
    tg_tok = (
        settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        if settings.TELEGRAM_BOT_TOKEN
        else ""
    )
    if not tg_tok or "mock" in tg_tok.lower():
        errors.append("TELEGRAM_BOT_TOKEN: Required and cannot be a mock placeholder.")

    if settings.TELEGRAM_MODE == "webhook":
        wh_sec = (
            settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
            if settings.TELEGRAM_WEBHOOK_SECRET
            else ""
        )
        if not wh_sec or "mock" in wh_sec.lower():
            errors.append(
                "TELEGRAM_WEBHOOK_SECRET: Required when TELEGRAM_MODE=webhook "
                "and cannot be a mock placeholder."
            )

    if errors:
        bullet_list = "\n  - " + "\n  - ".join(errors)
        raise RuntimeError(
            f"Startup failed! {len(errors)} required environment variable(s) "
            f"missing or invalid for {settings.ENVIRONMENT} mode:{bullet_list}\n\n"
            "Please configure these variables in Render Dashboard or environment before starting."
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    return Settings()
