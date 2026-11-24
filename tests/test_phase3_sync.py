"""Tests for Phase 3: MIME cleaner, HTML sanitizer, Gmail client, and Sync engine."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.errors import ConflictError
from app.models.db import EmailListItem, UserRow
from app.services.email_cleaner import (
    clean_html_to_text,
    extract_attachments_metadata,
    is_bulk_or_noreply,
    sanitize_email_body,
    strip_quotes_and_signatures,
)
from app.services.gmail_client import HistoryExpiredError
from app.services.sync_service import SyncService


def test_clean_html_to_text() -> None:
    """Test converting raw HTML into clean normalized plain text."""
    html = """
    <html>
        <head><style>.ad { color: red; }</style><script>alert('pwn');</script></head>
        <body>
            <h1>Project Update</h1>
            <p>Here is the weekly update on the backend service.</p>
            <div>Action items are listed below.</div>
        </body>
    </html>
    """
    cleaned = clean_html_to_text(html)
    assert "alert" not in cleaned
    assert ".ad {" not in cleaned
    assert "Project Update" in cleaned
    assert "Here is the weekly update" in cleaned


def test_strip_quotes_and_signatures() -> None:
    """Test removing quoted replies and common sign-offs."""
    email_text = """Hi Alice,

Thanks for sending this over. I agree with the proposal.

Best regards,
Bob

On Mon, Oct 24, 2026 at 10:00 AM Alice <alice@example.com> wrote:
> Do you think we should proceed with the new backend?
> Let me know your thoughts.
"""
    cleaned = strip_quotes_and_signatures(email_text)
    assert "Thanks for sending this over" in cleaned
    assert "On Mon, Oct 24, 2026 at 10:00 AM Alice" not in cleaned
    assert "Do you think we should proceed" not in cleaned


def test_sanitize_email_body_length_cap() -> None:
    """Test capping excessively long email bodies."""
    long_text = "A" * 20000
    capped = sanitize_email_body(long_text, is_html=False, max_chars=5000)
    assert len(capped) < 6000
    assert "[... Email body truncated at 5000 characters ...]" in capped


def test_bulk_email_detection() -> None:
    """Test detection of bulk, automated, and newsletter mail."""
    assert is_bulk_or_noreply(sender="noreply@github.com") is True
    assert (
        is_bulk_or_noreply(
            sender="marketing@company.com",
            list_unsubscribe="<mailto:unsub@company.com>",
        )
        is True
    )
    assert is_bulk_or_noreply(sender="alice@startup.io") is False


def test_extract_attachments_metadata() -> None:
    """Test extracting attachment metadata without downloading bytes."""
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "dGVzdA=="}},
            {
                "filename": "invoice_october.pdf",
                "mimeType": "application/pdf",
                "body": {"size": 1048576, "attachmentId": "att_12345"},
            },
        ]
    }
    attachments = extract_attachments_metadata(payload)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "invoice_october.pdf"
    assert attachments[0]["mime_type"] == "application/pdf"
    assert attachments[0]["size_bytes"] == 1048576


@pytest.mark.asyncio
async def test_sync_lock_conflict() -> None:
    """Test that concurrent sync attempts raise ConflictError."""
    mock_user_repo = MagicMock()
    mock_user_repo.get_by_id = AsyncMock(
        return_value=UserRow(
            id=uuid4(),
            google_sub="sub-1",
            email="test@example.com",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    # Lock acquisition returns False (already running)
    mock_user_repo.acquire_sync_lock = AsyncMock(return_value=False)

    sync_svc = SyncService(
        user_repo=mock_user_repo,
        email_repo=MagicMock(),
        auth_service=MagicMock(),
    )

    with pytest.raises(ConflictError):
        await sync_svc.sync_user_emails(uuid4())


@pytest.mark.asyncio
async def test_incremental_sync_falls_back_on_expired_history() -> None:
    """Test that 404 from history.list triggers fallback to initial sync."""
    user_id = uuid4()
    mock_user = UserRow(
        id=user_id,
        google_sub="sub-1",
        email="test@example.com",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    mock_user_repo = MagicMock()
    mock_user_repo.get_by_id = AsyncMock(return_value=mock_user)
    mock_user_repo.acquire_sync_lock = AsyncMock(return_value=True)
    mock_user_repo.release_sync_lock = AsyncMock()
    mock_user_repo.get_sync_state = AsyncMock(
        return_value=MagicMock(last_history_id="old_history_999")
    )
    mock_user_repo.update_sync_progress = AsyncMock()

    mock_auth_svc = MagicMock()
    mock_auth_svc.get_valid_access_token = AsyncMock(return_value="valid_token")

    sync_svc = SyncService(
        user_repo=mock_user_repo,
        email_repo=MagicMock(),
        auth_service=mock_auth_svc,
    )

    with (
        patch.object(SyncService, "_incremental_sync", side_effect=HistoryExpiredError("404")),
        patch.object(
            SyncService,
            "_initial_sync",
            new_callable=AsyncMock,
            return_value=(5, "new_history_1001"),
        ),
    ):
        result = await sync_svc.sync_user_emails(user_id)
        assert result["synced"] == 5
        assert result["history_id"] == "new_history_1001"
        mock_user_repo.release_sync_lock.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_list_emails_excludes_body_clean(client: AsyncClient) -> None:
    """Test GET /api/v1/emails returns EmailListItem without body_clean."""
    user_id = uuid4()
    now = datetime.now(UTC)
    mock_user = UserRow(
        id=user_id,
        google_sub="sub-1",
        email="user@example.com",
        created_at=now,
        updated_at=now,
    )

    mock_items = [
        EmailListItem(
            id=uuid4(),
            user_id=user_id,
            gmail_msg_id="msg_123",
            thread_id="th_123",
            sender="Sender <sender@example.com>",
            sender_email="sender@example.com",
            subject="Quarterly Review",
            received_at=now,
            snippet="Please find attached the review",
            has_attachments=True,
            labels=["INBOX"],
        )
    ]

    with (
        patch(
            "app.services.auth_service.AuthService.authenticate_session",
            new_callable=AsyncMock,
            return_value=mock_user,
        ),
        patch(
            "app.db.repos.email_repo.EmailRepo.list_by_user",
            new_callable=AsyncMock,
            return_value=mock_items,
        ),
    ):
        res = await client.get("/api/v1/emails", cookies={"session_token": "valid_session"})
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert "body_clean" not in data[0]
        assert data[0]["subject"] == "Quarterly Review"
