"""Tests for Phase 0: Configuration, Health probes, Middleware, and Logging."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import Settings
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
