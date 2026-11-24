"""Tests for Phase 2: Token Vault, Key Rotation, Google OAuth, Sessions, CSRF, and Deletion."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ValidationError
from app.core.vault import TokenVault, TokenVaultError
from app.models.db import UserRow
from app.services.auth_service import AuthService


def test_token_vault_encryption_roundtrip() -> None:
    """Test AES-256-GCM encryption and decryption roundtrip."""
    settings = Settings(
        TOKEN_ENCRYPTION_KEY="01234567890123456789012345678901",
        TOKEN_KEY_VERSION=1,
    )
    vault = TokenVault(settings)
    plaintext_token = "1//04test_google_oauth_refresh_token_xyz"

    ciphertext_b64, ver = vault.encrypt(plaintext_token)
    assert ver == 1
    assert ciphertext_b64 != plaintext_token

    decrypted = vault.decrypt(ciphertext_b64, key_version=1)
    assert decrypted == plaintext_token


def test_token_vault_key_rotation_support() -> None:
    """Test decrypting with secondary key and re-encrypting with new primary key."""
    settings = Settings(
        TOKEN_ENCRYPTION_KEY="01234567890123456789012345678902",
        TOKEN_KEY_VERSION=2,
        TOKEN_SECONDARY_KEYS={
            1: SecretStr("01234567890123456789012345678901"),
        },
    )
    vault = TokenVault(settings)

    # Encrypt with version 1 (old key)
    vault_v1 = TokenVault(
        Settings(
            TOKEN_ENCRYPTION_KEY="01234567890123456789012345678901",
            TOKEN_KEY_VERSION=1,
        )
    )
    ciphertext_v1, _ = vault_v1.encrypt("my_secret_token", key_version=1)

    # Vault with key version 2 can decrypt v1 payload via secondary keys
    decrypted_from_v1 = vault.decrypt(ciphertext_v1, key_version=1)
    assert decrypted_from_v1 == "my_secret_token"

    # Re-encrypt with active version 2
    ciphertext_v2, ver2 = vault.encrypt(decrypted_from_v1)
    assert ver2 == 2
    assert vault.decrypt(ciphertext_v2, key_version=2) == "my_secret_token"


def test_token_vault_invalid_key_length() -> None:
    """Ensure invalid key lengths raise TokenVaultError."""
    with pytest.raises(TokenVaultError):
        TokenVault(Settings(TOKEN_ENCRYPTION_KEY="too_short"))


def test_pkce_and_state_verification() -> None:
    """Test PKCE generation and signed state verification."""
    mock_repo = MagicMock()
    service = AuthService(mock_repo)

    verifier, challenge = service.generate_pkce_pair()
    assert len(verifier) >= 43
    assert len(challenge) >= 43

    state = service.create_signed_state(client_state="dest=/dashboard", code_verifier=verifier)
    client_state, recovered_verifier = service.verify_signed_state(state)
    assert client_state == "dest=/dashboard"
    assert recovered_verifier == verifier

    # Tampered state should fail verification
    with pytest.raises(ValidationError):
        service.verify_signed_state(state + "tampered")


@pytest.mark.asyncio
async def test_auth_google_start_endpoint(client: AsyncClient) -> None:
    """Test /api/v1/auth/google/start returns authorization URL and sets verifier cookie."""
    res = await client.get("/api/v1/auth/google/start")
    assert res.status_code == 200
    data = res.json()
    assert "accounts.google.com" in data["authorization_url"]
    assert "oauth_verifier" in res.cookies


@pytest.mark.asyncio
async def test_auth_google_callback_flow(client: AsyncClient) -> None:
    """Test Google OAuth callback flow with mocked token exchange."""
    mock_user_id = uuid4()
    now = datetime.now(UTC)
    mock_user = UserRow(
        id=mock_user_id,
        google_sub="google-sub-999",
        email="newuser@example.com",
        name="New User",
        timezone="UTC",
        locale="en",
        created_at=now,
        updated_at=now,
    )

    auth_svc = AuthService(MagicMock(), settings=Settings(ENVIRONMENT="test"))
    valid_state = auth_svc.create_signed_state()

    with (
        patch.object(
            AuthService,
            "exchange_code_for_tokens",
            new_callable=AsyncMock,
            return_value={
                "access_token": "ya29.mock_access_token",
                "refresh_token": "1//0mock_refresh_token",
                "id_token": "mock_id_token",
                "expires_in": 3600,
                "scope": "openid email",
            },
        ),
        patch.object(
            AuthService,
            "verify_google_id_token",
            return_value={
                "sub": "google-sub-999",
                "email": "newuser@example.com",
                "name": "New User",
            },
        ),
        patch(
            "app.db.repos.user_repo.UserRepo.upsert_user",
            new_callable=AsyncMock,
            return_value=mock_user,
        ),
        patch("app.db.repos.user_repo.UserRepo.upsert_oauth_token", new_callable=AsyncMock),
        patch("app.services.auth_service.AuthService._cache_access_token", new_callable=AsyncMock),
        patch(
            "app.db.repos.user_repo.UserRepo.create_session",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
    ):
        res = await client.get(
            f"/api/v1/auth/google/callback?code=mock_code&state={valid_state}",
            cookies={"oauth_verifier": "mock_verifier_xyz"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "authenticated"
        assert data["user"]["email"] == "newuser@example.com"
        assert "session_token" in res.cookies
        assert "csrf_token" in res.cookies


@pytest.mark.asyncio
async def test_get_current_user_unauthorized(client: AsyncClient) -> None:
    """Test that /api/v1/me returns 401 when unauthenticated."""
    res = await client.get("/api/v1/me")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_csrf_validation_fails_on_post(client: AsyncClient) -> None:
    """Test that POST /api/v1/me/preferences fails with 403 when CSRF token is missing."""
    now = datetime.now(UTC)
    mock_user = UserRow(
        id=uuid4(),
        google_sub="sub-123",
        email="user@example.com",
        created_at=now,
        updated_at=now,
    )

    with patch(
        "app.services.auth_service.AuthService.authenticate_session",
        new_callable=AsyncMock,
        return_value=mock_user,
    ):
        # With session cookie but missing CSRF
        client.cookies.set("session_token", "valid_session", domain="testserver")
        res = await client.patch("/api/v1/me/preferences", json={"timezone": "Europe/London"})
        assert res.status_code == 403
        assert res.json()["code"] == "FORBIDDEN"
