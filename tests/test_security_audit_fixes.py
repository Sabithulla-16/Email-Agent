"""Security audit regression tests: mass assignment, secret stripping, CSRF, SSRF redirects."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.deps import get_auth_service, get_current_user, get_user_repo
from app.main import app
from app.models.db import EmailRow, UserRow
from app.services.unsubscribe_service import UnsubscribeService


@pytest.mark.asyncio
async def test_preferences_blocks_mass_assignment_and_strips_encrypted_key(client):
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    fake_user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="u@test.com",
        name="User",
        prefs={"tone": "friendly", "groq_api_key_encrypted": "enc:v1:secret"},
        feature_flags={"inbox_qa": False},
        created_at=now,
        updated_at=now,
    )

    mock_user_repo = MagicMock()
    mock_user_repo.update_user = AsyncMock(return_value=fake_user)

    target_app = getattr(getattr(client, "_transport", None), "app", app)
    target_app.dependency_overrides[get_current_user] = lambda: fake_user
    target_app.dependency_overrides[get_user_repo] = lambda: mock_user_repo
    try:
        # 1. GET /me must never expose groq_api_key_encrypted
        me_res = await client.get("/api/v1/me")
        assert me_res.status_code == 200
        assert "groq_api_key_encrypted" not in me_res.json()["prefs"]

        # 2. PATCH /me/preferences must ignore feature_flags & next_digest_at
        patch_res = await client.patch(
            "/api/v1/me/preferences",
            headers={"Authorization": "Bearer test-token"},
            json={
                "prefs": {"tone": "formal"},
                "feature_flags": {"inbox_qa": True},
                "next_digest_at": "2020-01-01T00:00:00Z",
            },
        )
        assert patch_res.status_code == 200
        assert "groq_api_key_encrypted" not in patch_res.json()["prefs"]

        sent_update = mock_user_repo.update_user.call_args[0][1]
        assert sent_update.feature_flags is None
        assert sent_update.next_digest_at is None
        assert sent_update.prefs["groq_api_key_encrypted"] == "enc:v1:secret"
        assert sent_update.prefs["tone"] == "formal"
    finally:
        target_app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_logout_enforces_csrf_on_cookie_session(client):
    mock_auth = MagicMock()
    mock_auth.logout_session = AsyncMock()
    target_app = getattr(getattr(client, "_transport", None), "app", app)
    target_app.dependency_overrides[get_auth_service] = lambda: mock_auth
    try:
        res = await client.post("/api/v1/auth/logout")
        assert res.status_code == 403
        mock_auth.logout_session.assert_not_called()
    finally:
        target_app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unsubscribe_blocks_ssrf_redirect():
    user_id = uuid.uuid4()
    email_id = uuid.uuid4()
    now = datetime.now(UTC)

    email_repo = MagicMock()
    email_repo.get_by_id = AsyncMock(
        return_value=EmailRow(
            id=email_id,
            user_id=user_id,
            gmail_msg_id="gm1",
            thread_id="th1",
            sender="Attacker",
            sender_email="attacker@evil.example",
            subject="Promo",
            received_at=now,
            body_clean="Click",
            list_unsubscribe="<https://evil.example/unsub>",
            created_at=now,
            updated_at=now,
        )
    )
    email_repo.list_newsletters_by_user = AsyncMock(return_value=[])
    auth_svc = MagicMock()
    auth_svc.get_valid_access_token = AsyncMock(return_value="tok")

    redirect_resp = httpx.Response(
        302,
        headers={"location": "http://127.0.0.1:8000/api/v1/cron/trigger"},
    )

    with (
        patch("app.services.unsubscribe_service.is_safe_public_url") as mock_safe,
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=redirect_resp)),
        patch("app.services.unsubscribe_service.GmailClient") as mock_gmail_cls,
    ):
        # Allow initial URL, block the 127.0.0.1 redirect target
        mock_safe.side_effect = [True, False]
        mock_gmail_cls.return_value.archive_message = AsyncMock(return_value={})

        svc = UnsubscribeService(email_repo=email_repo, auth_service=auth_svc)
        res = await svc.unsubscribe_and_archive(user_id=user_id, email_id=email_id)

    assert res["unsubscribed"] is False
    assert res["method"] == "none"
