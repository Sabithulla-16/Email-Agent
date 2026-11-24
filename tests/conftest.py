"""Pytest configuration and test fixtures."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import app.db.client as db_client_mod
import app.queue.redis as redis_mod
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def test_settings() -> Settings:
    """Isolated settings fixture for tests."""
    return Settings(
        ENVIRONMENT="test",
        DEBUG=True,
        APP_SECRET_KEY="test-app-secret-key-that-is-at-least-32-chars-long",
        TOKEN_ENCRYPTION_KEY="test-aes-key-32-characters-exact",
        TOKEN_KEY_VERSION=1,
        SUPABASE_URL="https://test-ref.supabase.co",
        SUPABASE_SECRET_KEY="test_mock_secret_key",
        REDIS_URL="redis://localhost:6379/15",
        GROQ_API_KEY="test_mock_groq_key",
        GOOGLE_CLIENT_ID="test-client-id.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="test-client-secret",
        TELEGRAM_BOT_TOKEN="123456:test-bot-token",
        TELEGRAM_WEBHOOK_SECRET="test-webhook-secret-token",
    )


@pytest.fixture
async def client(test_settings: Settings) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client with mocked DB and Redis."""
    mock_supabase = MagicMock()
    mock_redis = AsyncMock()

    db_client_mod._supabase_client = mock_supabase
    redis_mod._redis_pool = mock_redis

    with (
        patch("app.main.init_supabase", new_callable=AsyncMock, return_value=mock_supabase),
        patch("app.main.init_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch("app.main.close_supabase", new_callable=AsyncMock),
        patch("app.main.close_redis", new_callable=AsyncMock),
    ):
        app = create_app(test_settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
