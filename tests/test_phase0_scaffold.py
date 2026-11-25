"""Tests for Phase 0: Configuration, Health probes, Middleware, and Logging."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import Settings, validate_startup_config
from app.core.logging import pii_masking_processor


def test_settings_secret_masking(test_settings: Settings) -> None:
    """Ensure secrets are never revealed via standard str() or repr()."""
    secret_str = str(test_settings.SUPABASE_SECRET_KEY)
    assert "test_mock_secret_key" not in secret_str
    assert "**********" in secret_str
    assert test_settings.SUPABASE_SECRET_KEY.get_secret_value() == "test_mock_secret_key"


def test_pii_log_masking() -> None:
    """Ensure sensitive keys in log payloads are redacted."""
    event = {
        "event": "user_login",
        "email": "user@example.com",
        "token": "secret_session_token_123",
        "nested": {
            "refresh_token": "google_refresh_token_abc",
            "safe_key": "safe_value",
        },
    }
    masked = pii_masking_processor(None, "info", event)
    assert masked["token"] == "[REDACTED]"
    assert masked["nested"]["refresh_token"] == "[REDACTED]"  # type: ignore[index]
    assert masked["nested"]["safe_key"] == "safe_value"  # type: ignore[index]


def test_validate_startup_config_dev_and_test_pass(test_settings: Settings) -> None:
    """In test or dev mode, placeholder values do not raise."""
    validate_startup_config(test_settings)


def test_validate_startup_config_production_fails_fast() -> None:
    """In production mode, missing/insecure configs must fail fast with clear errors."""
    bad_prod_settings = Settings(
        ENVIRONMENT="production",
        APP_SECRET_KEY="insecure-dev-key",
        TOKEN_ENCRYPTION_KEY="short",
        SUPABASE_URL="http://localhost:54321",
        SUPABASE_SECRET_KEY="dummy",
        GROQ_API_KEY="mock",
        GOOGLE_CLIENT_ID="mock",
        GOOGLE_CLIENT_SECRET="mock",
        GOOGLE_REDIRECT_URI="http://localhost:8000/callback",
        PUBLIC_BASE_URL="http://localhost:8000",
        TELEGRAM_BOT_TOKEN="123456:mock",
        TELEGRAM_MODE="webhook",
        TELEGRAM_WEBHOOK_SECRET="mock",
    )
    with pytest.raises(RuntimeError) as exc_info:
        validate_startup_config(bad_prod_settings)

    err_msg = str(exc_info.value)
    assert "APP_SECRET_KEY" in err_msg
    assert "TOKEN_ENCRYPTION_KEY" in err_msg
    assert "SUPABASE_URL" in err_msg
    assert "SUPABASE_SECRET_KEY" in err_msg
    assert "GROQ_API_KEY" in err_msg
    assert "GOOGLE_CLIENT_ID" in err_msg
    assert "GOOGLE_CLIENT_SECRET" in err_msg
    assert "GOOGLE_REDIRECT_URI" in err_msg
    assert "PUBLIC_BASE_URL" in err_msg
    assert "TELEGRAM_BOT_TOKEN" in err_msg
    assert "TELEGRAM_WEBHOOK_SECRET" in err_msg


def test_validate_startup_config_production_valid() -> None:
    """In production mode, fully valid configs pass validation."""
    good_prod_settings = Settings(
        ENVIRONMENT="production",
        APP_SECRET_KEY="a" * 32,
        TOKEN_ENCRYPTION_KEY="prod-test-encryption-key-32-byte",
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SECRET_KEY="real_secret_role_key_value",
        REDIS_URL="rediss://default:password@host:6379",
        GROQ_API_KEY="real_groq_production_key",
        GOOGLE_CLIENT_ID="real-client-id.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="real-client-secret-val",
        GOOGLE_REDIRECT_URI="https://email-agent.onrender.com/api/v1/auth/google/callback",
        PUBLIC_BASE_URL="https://email-agent.onrender.com",
        TELEGRAM_BOT_TOKEN="987654321:AAECorrectProductionToken",
        TELEGRAM_MODE="webhook",
        TELEGRAM_WEBHOOK_SECRET="secure_random_telegram_webhook_secret_12345",
    )
    # Should not raise
    validate_startup_config(good_prod_settings)


@pytest.mark.asyncio
async def test_healthz_endpoint(client: AsyncClient) -> None:
    """Check that /healthz returns 200 OK and expected payload."""
    res = await client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "telegram" in data
    assert "x-request-id" in res.headers
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_readyz_healthy(client: AsyncClient) -> None:
    """Check that /readyz returns 200 when all dependencies report healthy."""
    with (
        patch(
            "app.main.check_supabase_health",
            new_callable=AsyncMock,
            return_value=(True, "connected"),
        ),
        patch(
            "app.main.check_redis_health",
            new_callable=AsyncMock,
            return_value=(True, "connected"),
        ),
    ):
        res = await client.get("/readyz")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ready"
        assert data["services"]["database"]["healthy"] is True
        assert data["services"]["redis"]["healthy"] is True


@pytest.mark.asyncio
async def test_readyz_degraded(client: AsyncClient) -> None:
    """Check that /readyz returns 503 when any dependency is degraded."""
    with (
        patch(
            "app.main.check_supabase_health",
            new_callable=AsyncMock,
            return_value=(False, "Connection timeout"),
        ),
        patch(
            "app.main.check_redis_health",
            new_callable=AsyncMock,
            return_value=(True, "connected"),
        ),
    ):
        res = await client.get("/readyz")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["services"]["database"]["healthy"] is False
        assert data["services"]["redis"]["healthy"] is True


@pytest.mark.asyncio
async def test_request_id_header_propagation(client: AsyncClient) -> None:
    """Test that custom X-Request-ID headers are honored and returned."""
    custom_id = "test-custom-request-id-42"
    res = await client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert res.status_code == 200
    assert res.headers["x-request-id"] == custom_id
