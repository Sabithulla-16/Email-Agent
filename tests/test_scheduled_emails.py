"""Tests for /schedule interactive flow, time parsing, encrypted storage, and auto-dispatch."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.vault import ENC_PREFIX, TokenVault
from app.models.db import TelegramLinkRow, UserRow
from app.services.scheduled_email_service import (
    ScheduledEmailService,
    parse_schedule_time,
)
from app.services.telegram_service import (
    _PENDING_SCHEDULE_EDIT_STATES,
    _PENDING_SCHEDULE_STATES,
    TelegramService,
)


def test_parse_schedule_time_ist():
    # 2026-11-25 19:30 IST = 14:00 UTC
    now_utc = datetime(2026, 11, 25, 14, 0, 0, tzinfo=UTC)

    # Relative
    assert parse_schedule_time("in 30m", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 25, 14, 30, 0, tzinfo=UTC
    )
    assert parse_schedule_time("2h", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 25, 16, 0, 0, tzinfo=UTC
    )

    # Same day 21:30 IST = 16:00 UTC
    assert parse_schedule_time("21:30", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 25, 16, 0, 0, tzinfo=UTC
    )
    assert parse_schedule_time("9:30 pm", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 25, 16, 0, 0, tzinfo=UTC
    )

    # Tomorrow 09:00 IST = 2026-11-26 03:30 UTC
    assert parse_schedule_time("tomorrow 09:00", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 26, 3, 30, 0, tzinfo=UTC
    )

    # Explicit date
    assert parse_schedule_time("2026-11-27 14:00", "Asia/Kolkata", now=now_utc) == datetime(
        2026, 11, 27, 8, 30, 0, tzinfo=UTC
    )

    # Invalid
    assert parse_schedule_time("nonsense", "Asia/Kolkata", now=now_utc) is None


@pytest.mark.asyncio
async def test_scheduled_email_encrypts_and_dispatches():
    vault = TokenVault()
    user_id = uuid.uuid4()
    sched_id = uuid.uuid4()
    now = datetime.now(UTC)
    due_at = now - timedelta(minutes=1)

    stored_payload: dict = {}

    def _make_exec(data):
        r = MagicMock()
        r.data = data
        return AsyncMock(return_value=r)

    sched_table = MagicMock()

    def _insert(payload):
        stored_payload.update(payload)
        row = {"id": str(sched_id), "created_at": now.isoformat(), **payload}
        q = MagicMock()
        q.execute = _make_exec([row])
        return q

    sched_table.insert = _insert

    client = MagicMock()
    client.table = lambda name: sched_table if name == "scheduled_emails" else MagicMock()

    svc = ScheduledEmailService(client=client, vault=vault)
    created = await svc.create_scheduled_email(
        user_id=user_id,
        recipient="bob@example.com",
        subject="Project Update",
        body="Here is the update.",
        scheduled_at=due_at,
    )

    assert stored_payload["recipient_enc"].startswith(ENC_PREFIX)
    assert stored_payload["subject_enc"].startswith(ENC_PREFIX)
    assert stored_payload["body_enc"].startswith(ENC_PREFIX)
    assert created["recipient"] == "bob@example.com"
    assert created["subject"] == "Project Update"

    # Now test dispatch_due_scheduled_emails
    due_row = {"id": str(sched_id), "user_id": str(user_id), **stored_payload}

    select_chain = MagicMock()
    select_chain.eq.return_value = select_chain
    select_chain.lte.return_value = select_chain
    select_chain.order.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute = _make_exec([due_row])
    sched_table.select = lambda *_args: select_chain

    update_chain = MagicMock()
    update_chain.eq.return_value = update_chain
    update_chain.execute = _make_exec([{"id": str(sched_id)}])
    sched_table.update = lambda *_args: update_chain

    with (
        patch("app.services.scheduled_email_service.UserRepo") as mock_user_repo_cls,
        patch("app.services.scheduled_email_service.AuthService") as mock_auth_cls,
        patch("app.services.scheduled_email_service.TelegramRepo") as mock_tg_repo_cls,
        patch("app.services.scheduled_email_service.TelegramClient") as mock_tg_client_cls,
        patch("app.services.scheduled_email_service.GmailClient") as mock_gmail_cls,
    ):
        mock_user_repo_cls.return_value.get_by_id = AsyncMock(
            return_value=UserRow(
                id=user_id,
                google_sub="sub1",
                email="alice@example.com",
                created_at=now,
                updated_at=now,
            )
        )
        mock_auth_cls.return_value.get_valid_access_token = AsyncMock(return_value="tok")
        mock_gmail_cls.return_value.send_raw_message = AsyncMock(return_value={"id": "gm_sent_1"})
        mock_tg_repo_cls.return_value.get_link_by_user_id = AsyncMock(
            return_value=TelegramLinkRow(
                user_id=user_id,
                chat_id=777,
                is_active=True,
                linked_at=now,
                updated_at=now,
            )
        )
        mock_tg_client_cls.return_value.send_message = AsyncMock(return_value={"message_id": 1})

        dispatch_res = await svc.dispatch_due_scheduled_emails()

    assert dispatch_res == {"processed": 1, "sent": 1, "failed": 0}
    mock_gmail_cls.return_value.send_raw_message.assert_awaited_once()
    mock_tg_client_cls.return_value.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_schedule_3_step_flow():
    chat_id = 888999
    _PENDING_SCHEDULE_STATES.pop(chat_id, None)
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    telegram_repo = MagicMock()
    telegram_repo.get_link_by_chat_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=chat_id,
            is_active=True,
            linked_at=now,
            updated_at=now,
        )
    )
    telegram_repo.get_and_clear_pending_interaction = AsyncMock(return_value=None)

    user_repo = MagicMock()
    user_repo.get_by_id = AsyncMock(
        return_value=UserRow(
            id=user_id,
            google_sub="sub1",
            email="alice@example.com",
            timezone="Asia/Kolkata",
            created_at=now,
            updated_at=now,
        )
    )

    tg_client = MagicMock()
    tg_client.send_message = AsyncMock(return_value={"message_id": 10})
    tg_client.delete_message = AsyncMock(return_value=True)

    svc = TelegramService(
        telegram_repo=telegram_repo,
        user_repo=user_repo,
        email_repo=MagicMock(),
        draft_repo=MagicMock(),
        approval_service=MagicMock(),
        notification_service=MagicMock(),
        telegram_client=tg_client,
    )
    svc._finalize_scheduled_email = AsyncMock()  # type: ignore[method-assign]

    # Step 1: /schedule -> asks for content
    await svc._handle_message({"chat": {"id": chat_id}, "text": "/schedule"})
    assert chat_id in _PENDING_SCHEDULE_STATES

    # Step 2: user sends content -> asks for recipient
    await svc._handle_message({"chat": {"id": chat_id}, "text": "Remind him about tomorrow's demo"})
    assert _PENDING_SCHEDULE_STATES[chat_id]["content"] == "Remind him about tomorrow's demo"

    # Step 3: user sends recipient -> asks for timing
    await svc._handle_message({"chat": {"id": chat_id}, "text": "bob@company.com"})
    assert _PENDING_SCHEDULE_STATES[chat_id]["recipient"] == "bob@company.com"

    # Step 4: user sends timing -> enters preview via _finalize_scheduled_email
    await svc._handle_message({"chat": {"id": chat_id}, "text": "in 30m"})
    svc._finalize_scheduled_email.assert_awaited_once()
    _PENDING_SCHEDULE_STATES.pop(chat_id, None)


@pytest.mark.asyncio
async def test_schedule_preview_revise_accept_and_manage_edit():
    chat_id = 444555
    _PENDING_SCHEDULE_STATES.pop(chat_id, None)
    _PENDING_SCHEDULE_EDIT_STATES.pop(chat_id, None)
    user_id = uuid.uuid4()
    sched_id = uuid.uuid4()
    now = datetime.now(UTC)

    telegram_repo = MagicMock()
    telegram_repo.get_link_by_chat_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=chat_id,
            is_active=True,
            linked_at=now,
            updated_at=now,
        )
    )
    telegram_repo.get_and_clear_pending_interaction = AsyncMock(return_value=None)

    user_repo = MagicMock()
    user_repo.get_by_id = AsyncMock(
        return_value=UserRow(
            id=user_id,
            google_sub="sub1",
            email="alice@example.com",
            name="Alice",
            timezone="Asia/Kolkata",
            created_at=now,
            updated_at=now,
        )
    )

    tg_client = MagicMock()
    tg_client.send_message = AsyncMock(return_value={"message_id": 20})
    tg_client.edit_message_text = AsyncMock(return_value={"message_id": 20})
    tg_client.delete_message = AsyncMock(return_value=True)

    svc = TelegramService(
        telegram_repo=telegram_repo,
        user_repo=user_repo,
        email_repo=MagicMock(),
        draft_repo=MagicMock(),
        approval_service=MagicMock(),
        notification_service=MagicMock(),
        telegram_client=tg_client,
    )

    with (
        patch(
            "app.services.telegram_service.ScheduledEmailService.compose_subject_and_body",
            new=AsyncMock(return_value=("Initial Subject", "Initial Body")),
        ),
        patch(
            "app.services.telegram_service.ScheduledEmailService.revise_subject_and_body",
            new=AsyncMock(return_value=("Revised Subject", "Revised Body")),
        ),
        patch(
            "app.services.telegram_service.ScheduledEmailService.create_scheduled_email",
            new=AsyncMock(return_value={"id": str(sched_id)}),
        ) as mock_create,
        patch(
            "app.services.telegram_service.ScheduledEmailService.update_scheduled_email",
            new=AsyncMock(return_value={"id": str(sched_id), "recipient": "new@corp.com"}),
        ) as mock_update,
    ):
        # 1. Enter preview
        await svc._finalize_scheduled_email(
            chat_id=chat_id,
            user_id=user_id,
            recipient="bob@corp.com",
            notes="Hello",
            scheduled_at=now + timedelta(hours=1),
        )
        assert _PENDING_SCHEDULE_STATES[chat_id]["stage"] == "review"

        # 2. Tap Revise -> sends prompt -> user replies with instruction
        await svc._handle_callback_query(
            {"data": "sched_preview:revise", "message": {"chat": {"id": chat_id}, "message_id": 20}}
        )
        assert _PENDING_SCHEDULE_STATES[chat_id]["awaiting_revision"] is True

        await svc._handle_message({"chat": {"id": chat_id}, "text": "Make it more concise"})
        assert _PENDING_SCHEDULE_STATES[chat_id]["subject"] == "Revised Subject"

        # 3. Tap Accept & Schedule -> persists to DB
        await svc._handle_callback_query(
            {"data": "sched_preview:accept", "message": {"chat": {"id": chat_id}, "message_id": 20}}
        )
        assert chat_id not in _PENDING_SCHEDULE_STATES
        mock_create.assert_awaited_once()

        # 4. Post-schedule /manage edit recipient
        await svc._handle_callback_query(
            {
                "data": f"sched_edit:recipient:{sched_id}",
                "message": {"chat": {"id": chat_id}, "message_id": 21},
            }
        )
        assert chat_id in _PENDING_SCHEDULE_EDIT_STATES

        await svc._handle_message({"chat": {"id": chat_id}, "text": "new@corp.com"})
        assert chat_id not in _PENDING_SCHEDULE_EDIT_STATES
        mock_update.assert_awaited_once()
