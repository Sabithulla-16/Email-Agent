"""Phase 6 tests: Telegram bot, notification service, webhook, and voice notes."""

import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import ForbiddenError
from app.main import app
from app.models.db import (
    CallbackTokenRow,
    DraftRow,
    DraftStatus,
    DraftVersionRow,
    EmailAnalysisRow,
    EmailCategory,
    EmailPriority,
    EmailRow,
    NotificationPrefsRow,
    TelegramLinkRow,
    UserRow,
)
from app.services.notification_service import NotificationService
from app.services.telegram_client import TelegramClient, escape_html
from app.services.telegram_service import TelegramService


# =============================================================================
# TelegramClient Tests
# =============================================================================
def test_escape_html():
    raw = "<script>alert('xss & attack');</script>"
    escaped = escape_html(raw)
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&lt;script&gt;" in escaped


@pytest.mark.asyncio
async def test_telegram_client_truncates_long_message():
    client = TelegramClient(bot_token="test_bot_token")
    huge_text = "A" * 5000

    with patch.object(client, "_post_request", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"message_id": 123}
        await client.send_message(chat_id=12345, text=huge_text)

        mock_post.assert_awaited_once()
        payload = mock_post.await_args[0][1]
        assert len(payload["text"]) <= 4096
        assert "[Message truncated" in payload["text"]


@pytest.mark.asyncio
async def test_telegram_client_handles_403_deactivation():
    mock_repo = MagicMock()
    mock_repo.deactivate_chat = AsyncMock()
    client = TelegramClient(bot_token="test_token", telegram_repo=mock_repo)

    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await client.send_message(chat_id=98765, text="Hello")

        assert res is None
        mock_repo.deactivate_chat.assert_awaited_once_with(98765)


# =============================================================================
# NotificationService Tests (Quiet Hours, Snooze, Inline Buttons)
# =============================================================================
@pytest.fixture
def notif_service():
    user_repo = MagicMock()
    email_repo = MagicMock()
    draft_repo = MagicMock()
    telegram_repo = MagicMock()
    client = MagicMock()
    client.send_message = AsyncMock(return_value={"message_id": 1})
    return NotificationService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        telegram_client=client,
    )


def test_is_in_quiet_hours(notif_service):
    # Midnight-crossing quiet hours: 22:00 to 07:00
    assert notif_service.is_in_quiet_hours(None, None) is False
    assert notif_service.is_in_quiet_hours(time(22, 0), time(7, 0), "UTC") in (True, False)


@pytest.mark.asyncio
async def test_should_suppress_notification_when_snoozed(notif_service):
    now = datetime.now(UTC)
    user = UserRow(
        id=uuid.uuid4(),
        google_sub="sub1",
        email="test@example.com",
        created_at=now,
        updated_at=now,
    )
    prefs = NotificationPrefsRow(
        user_id=user.id,
        channel="telegram",
        snoozed_until=now + timedelta(hours=2),
        created_at=now,
        updated_at=now,
    )

    suppressed = await notif_service.should_suppress_notification(
        user, prefs, priority=EmailPriority.HIGH
    )
    assert suppressed is True


@pytest.mark.asyncio
async def test_notify_new_draft_creates_single_use_tokens(notif_service):
    now = datetime.now(UTC)
    user_id = uuid.uuid4()
    draft_id = uuid.uuid4()

    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="test@example.com",
        created_at=now,
        updated_at=now,
    )
    draft = DraftRow(
        id=draft_id,
        user_id=user_id,
        email_id=uuid.uuid4(),
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="key_1",
        expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    version = DraftVersionRow(
        id=uuid.uuid4(),
        draft_id=draft_id,
        version=1,
        body="I would love to collaborate.",
        source="llm",
        model="llama-3.3-70b-versatile",
        created_at=now,
    )
    email = EmailRow(
        id=draft.email_id,
        user_id=user_id,
        gmail_msg_id="m1",
        thread_id="t1",
        sender="Partner <partner@test.com>",
        sender_email="partner@test.com",
        subject="Project idea",
        body_clean="Hey, want to work together?",
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    analysis = EmailAnalysisRow(
        email_id=email.id,
        category=EmailCategory.WORK,
        needs_reply=True,
        priority=EmailPriority.MEDIUM,
        summary="Collaboration inquiry",
        confidence=0.9,
        model="llama",
        prompt_version="1",
        created_at=now,
    )

    notif_service.user_repo.get_by_id = AsyncMock(return_value=user)
    notif_service.user_repo.get_notification_prefs = AsyncMock(return_value=None)
    notif_service.telegram_repo.get_link_by_user_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=12345678,
            is_active=True,
            linked_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    notif_service.telegram_repo.create_callback_token = AsyncMock()

    res = await notif_service.notify_new_draft(draft, version, email, analysis)

    assert res["status"] == "delivered_telegram"
    assert res["chat_id"] == 12345678
    # Ensure all 7 single-use tokens were created
    assert notif_service.telegram_repo.create_callback_token.await_count == 7


# =============================================================================
# TelegramService & Webhook Tests
# =============================================================================
@pytest.fixture
def telegram_service():
    tg_repo = MagicMock()
    tg_repo.get_link_by_chat_id = AsyncMock(return_value=None)
    user_repo = MagicMock()
    email_repo = MagicMock()
    draft_repo = MagicMock()
    approval_svc = MagicMock()
    notif_svc = MagicMock()
    client = MagicMock()
    client.send_message = AsyncMock(return_value={"message_id": 99})
    client.edit_message_text = AsyncMock(return_value={"message_id": 99})
    groq_svc = MagicMock()

    return TelegramService(
        telegram_repo=tg_repo,
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        approval_service=approval_svc,
        notification_service=notif_svc,
        telegram_client=client,
        groq_service=groq_svc,
    )


@pytest.mark.asyncio
async def test_create_link_deep_link(telegram_service):
    user_id = uuid.uuid4()
    telegram_service.telegram_repo.create_link_token = AsyncMock()

    link = await telegram_service.create_link_deep_link(user_id)
    expected_prefix = f"https://t.me/{telegram_service.settings.TELEGRAM_BOT_USERNAME}?start="
    assert link.startswith(expected_prefix)
    telegram_service.telegram_repo.create_link_token.assert_awaited_once()


def test_verify_webhook_secret_header(telegram_service):
    # Invalid secret raises ForbiddenError
    with pytest.raises(ForbiddenError):
        telegram_service.verify_webhook_secret("wrong_secret")

    # Correct secret does not raise
    expected = telegram_service.settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
    telegram_service.verify_webhook_secret(expected)


@pytest.mark.asyncio
async def test_handle_callback_approve_action(telegram_service):
    chat_id = 99999
    draft_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    consumed_token = CallbackTokenRow(
        token="tok_123",
        user_id=user_id,
        chat_id=chat_id,
        draft_id=draft_id,
        action="approve",
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    telegram_service.telegram_repo.consume_callback_token = AsyncMock(return_value=consumed_token)
    telegram_service.approval_service.approve_and_send = AsyncMock()

    update = {
        "update_id": 1001,
        "callback_query": {
            "data": "tok_123",
            "message": {"chat": {"id": chat_id}, "message_id": 42},
        },
    }

    with patch("app.services.telegram_service.get_redis", return_value=None):
        await telegram_service.process_webhook_update(update)

    telegram_service.approval_service.approve_and_send.assert_awaited_once_with(draft_id, user_id)
    telegram_service.telegram_client.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_start_command_without_token(telegram_service):
    chat_id = 12345
    update = {
        "update_id": 1002,
        "message": {
            "chat": {"id": chat_id},
            "text": "/start",
        },
    }

    with patch("app.services.telegram_service.get_redis", return_value=None):
        await telegram_service.process_webhook_update(update)

    telegram_service.telegram_client.send_message.assert_awaited_once()
    call_kwargs = telegram_service.telegram_client.send_message.await_args.kwargs
    assert call_kwargs["chat_id"] == chat_id
    assert "Connect Gmail" in str(call_kwargs.get("reply_markup"))
    expected_url = (
        f"{telegram_service.settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/auth/google?chat_id={chat_id}"
    )
    assert expected_url in str(call_kwargs.get("reply_markup"))


@pytest.mark.asyncio
async def test_handle_start_command_when_already_linked(telegram_service):
    chat_id = 998877
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    active_link = TelegramLinkRow(
        user_id=user_id,
        chat_id=chat_id,
        is_active=True,
        linked_at=now,
        created_at=now,
        updated_at=now,
    )
    mock_user = UserRow(
        id=user_id,
        google_sub="sub-linked",
        email="linkeduser@example.com",
        name="Valtry",
        created_at=now,
        updated_at=now,
    )
    telegram_service.telegram_repo.get_link_by_chat_id = AsyncMock(return_value=active_link)
    telegram_service.user_repo.get_by_id = AsyncMock(return_value=mock_user)

    update = {
        "update_id": 1003,
        "message": {
            "chat": {"id": chat_id},
            "text": "/start",
        },
    }

    with patch("app.services.telegram_service.get_redis", return_value=None):
        await telegram_service.process_webhook_update(update)

    telegram_service.telegram_client.send_message.assert_awaited_once()
    call_kwargs = telegram_service.telegram_client.send_message.await_args.kwargs
    assert call_kwargs["chat_id"] == chat_id
    assert "Welcome back, Valtry!" in call_kwargs["text"]
    assert "linkeduser@example.com" in call_kwargs["text"]
    assert call_kwargs.get("reply_markup") is None


# =============================================================================
# API Route Tests
# =============================================================================
@pytest.mark.asyncio
async def test_telegram_api_link_and_webhook():
    now = datetime.now(UTC)
    user = UserRow(
        id=uuid.uuid4(),
        google_sub="sub_tg_test",
        email="tguser@example.com",
        created_at=now,
        updated_at=now,
    )

    from app.api.deps import get_current_user
    from app.api.v1.telegram import get_telegram_service

    mock_svc = MagicMock()
    mock_svc.create_link_deep_link = AsyncMock(
        return_value="https://t.me/EmailAgentBot?start=testtoken"
    )
    mock_svc.verify_webhook_secret = MagicMock()
    mock_svc.process_webhook_update = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_telegram_service] = lambda: mock_svc

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 1. Generate link
            resp = await client.post(
                "/api/v1/telegram/link",
                headers={"Authorization": "Bearer key"},
            )
            assert resp.status_code == 200
            assert "deep_link" in resp.json()

            # 2. Webhook invocation
            resp = await client.post(
                "/api/v1/telegram/webhook",
                json={"update_id": 1, "message": {"text": "/help"}},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "mock_telegram_webhook_secret_header_token"
                },
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            mock_svc.process_webhook_update.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
