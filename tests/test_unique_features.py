"""Tests for Features 2, 3, 4, and 5 (Radar, Unsubscribe, Encrypted Search, Voice Briefing)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.vault import TokenVault
from app.db.repos.email_repo import EmailRepo
from app.models.db import (
    EmailAnalysisRow,
    EmailCategory,
    EmailListItem,
    EmailPriority,
    EmailRow,
    NotificationPrefsRow,
    TelegramLinkRow,
    UserRow,
)
from app.services.digest_service import DigestService
from app.services.followup_service import FollowupService
from app.services.inbox_qa import InboxQAService
from app.services.telegram_service import TelegramService
from app.services.unsubscribe_service import (
    UnsubscribeService,
    is_safe_public_url,
    parse_list_unsubscribe,
)
from app.services.voice_service import (
    DEFAULT_VOICE_NAME,
    build_spoken_script,
    is_voice_enabled,
    resolve_voice_name,
)


# =============================================================================
# Feature 2: Decision & Commitment Radar
# =============================================================================
@pytest.mark.asyncio
async def test_radar_summary_combines_waiting_on_you_and_others():
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    email_id = uuid.uuid4()

    email_repo = MagicMock()
    email_repo.list_by_user = AsyncMock(
        return_value=[
            EmailListItem(
                id=email_id,
                user_id=user_id,
                gmail_msg_id="gm1",
                thread_id="th1",
                sender="Alice",
                sender_email="alice@example.com",
                subject="Need your sign-off",
                received_at=now,
            )
        ]
    )
    email_repo.get_analysis = AsyncMock(
        return_value=EmailAnalysisRow(
            email_id=email_id,
            category=EmailCategory.WORK,
            needs_reply=True,
            priority=EmailPriority.HIGH,
            summary="Alice needs approval on budget",
            confidence=0.9,
            model="gpt-oss-120b",
            prompt_version="v1",
            created_at=now,
        )
    )

    vault = TokenVault()
    commitments_res = MagicMock()
    commitments_res.data = [
        {
            "id": str(uuid.uuid4()),
            "promise": vault.encrypt_field("Send Q4 deck"),
            "promised_to": vault.encrypt_field("Bob"),
            "due_date": "2026-11-28",
        }
    ]
    followups_res = MagicMock()
    followups_res.data = []

    def _make_chain(res_obj):
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute = AsyncMock(return_value=res_obj)
        return chain

    client = MagicMock()
    client.table = lambda name: _make_chain(
        commitments_res if name == "commitments" else followups_res
    )

    svc = FollowupService(client=client)
    radar = await svc.get_radar_summary(user_id=user_id, email_repo=email_repo)

    assert len(radar["waiting_on_you"]) == 1
    assert radar["waiting_on_you"][0]["sender"] == "Alice"
    assert len(radar["waiting_on_others"]) == 1
    assert radar["waiting_on_others"][0]["label"] == "Send Q4 deck"


# =============================================================================
# Feature 3: One-Tap Newsletter & Promo Purge
# =============================================================================
def test_parse_list_unsubscribe_and_ssrf_guard():
    https_url, mailto = parse_list_unsubscribe(
        "<mailto:unsub@news.example.com>, <https://news.example.com/unsub?id=1>"
    )
    assert https_url == "https://news.example.com/unsub?id=1"
    assert mailto == "mailto:unsub@news.example.com"

    assert is_safe_public_url("http://insecure.example.com/unsub") is False
    assert is_safe_public_url("https://127.0.0.1/unsub") is False
    assert is_safe_public_url("https://localhost/unsub") is False


@pytest.mark.asyncio
async def test_unsubscribe_enforces_user_ownership():
    owner_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    email_id = uuid.uuid4()
    now = datetime.now(UTC)

    email_repo = MagicMock()
    email_repo.get_by_id = AsyncMock(
        return_value=EmailRow(
            id=email_id,
            user_id=owner_id,
            gmail_msg_id="gm1",
            thread_id="th1",
            sender="News",
            sender_email="news@example.com",
            subject="Weekly Digest",
            received_at=now,
            body_clean="Hello",
            list_unsubscribe="<https://news.example.com/unsub>",
            created_at=now,
            updated_at=now,
        )
    )
    auth_svc = MagicMock()
    svc = UnsubscribeService(email_repo=email_repo, auth_service=auth_svc)

    # Another user must not be able to unsubscribe/archive owner's email
    res = await svc.unsubscribe_and_archive(user_id=other_user_id, email_id=email_id)
    assert res["status"] == "not_found"


# =============================================================================
# Feature 4: Encrypted Semantic Search
# =============================================================================
@pytest.mark.asyncio
async def test_encrypted_search_decrypted():
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    vault = TokenVault()

    em1_id = uuid.uuid4()
    em2_id = uuid.uuid4()

    client = MagicMock()
    repo = EmailRepo(client, vault=vault)
    repo.list_by_user = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            EmailListItem(
                id=em1_id,
                user_id=user_id,
                gmail_msg_id="gm1",
                thread_id="th1",
                sender="IndiGo",
                sender_email="bookings@goindigo.in",
                subject="Flight Ticket Confirmation to Delhi",
                received_at=now,
                snippet="PNR ABC123",
            ),
            EmailListItem(
                id=em2_id,
                user_id=user_id,
                gmail_msg_id="gm2",
                thread_id="th2",
                sender="GitHub",
                sender_email="noreply@github.com",
                subject="Security alert",
                received_at=now,
                snippet="New login",
            ),
        ]
    )
    repo.get_analysis = AsyncMock(return_value=None)  # type: ignore[method-assign]

    qa = InboxQAService(email_repo=repo)
    hits = await qa.search_emails(user_id=user_id, query="flight delhi", limit=5)

    assert len(hits) == 1
    assert hits[0]["id"] == str(em1_id)
    assert "Delhi" in hits[0]["subject"]


# =============================================================================
# Feature 5: Voice-First Morning Briefing (Text + Voice) & /voice Command
# =============================================================================
def test_voice_prefs_and_script_builder():
    assert resolve_voice_name(None) == DEFAULT_VOICE_NAME
    assert resolve_voice_name({"voice_name": "prabhat"}) == "en-IN-PrabhatNeural"
    assert resolve_voice_name({"voice_name": "aria"}) == "en-US-AriaNeural"
    assert is_voice_enabled(None) is True
    assert is_voice_enabled({"voice_briefing": False}) is False

    script = build_spoken_script(
        user_name="<b>Sabit</b>",
        total_emails=3,
        total_pending=1,
        urgent_emails=["URGENT: Server down"],
        email_summaries=[
            {"sender": "Alice <a@b.com>", "subject": "Hi", "summary": "Meeting at 4 PM"}
        ],
    )
    assert "Good morning Sabit" in script
    assert "<b>" not in script
    assert "Meeting at 4 PM" in script


@pytest.mark.asyncio
async def test_morning_digest_sends_both_text_and_voice():
    user_id = uuid.uuid4()
    # 9:00 AM IST = 03:30 UTC
    now_9am_ist = datetime(2026, 11, 25, 3, 30, 0, tzinfo=UTC)
    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="u@test.com",
        name="Sabit",
        timezone="Asia/Kolkata",
        prefs={"voice_briefing": True, "voice_name": "prabhat"},
        created_at=now_9am_ist,
        updated_at=now_9am_ist,
    )
    prefs = NotificationPrefsRow(
        user_id=user_id,
        channel="telegram",
        digest_enabled=True,
        created_at=now_9am_ist,
        updated_at=now_9am_ist,
    )

    user_repo = MagicMock()
    user_repo.get_by_id = AsyncMock(return_value=user)
    user_repo.get_notification_prefs = AsyncMock(return_value=prefs)
    user_repo.update_user = AsyncMock(return_value=user)

    email_repo = MagicMock()
    email_repo.list_by_user_since = AsyncMock(
        return_value=[
            EmailListItem(
                id=uuid.uuid4(),
                user_id=user_id,
                gmail_msg_id="m1",
                thread_id="t1",
                sender="Boss",
                sender_email="boss@corp.com",
                subject="Status update",
                received_at=now_9am_ist,
                snippet="Please share status",
            )
        ]
    )
    email_repo.get_analysis = AsyncMock(return_value=None)

    draft_repo = MagicMock()
    draft_repo.list_by_user = AsyncMock(return_value=[])

    telegram_repo = MagicMock()
    telegram_repo.get_link_by_user_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=999,
            is_active=True,
            linked_at=now_9am_ist,
            updated_at=now_9am_ist,
        )
    )

    tg_client = MagicMock()
    tg_client.send_message = AsyncMock(return_value={"message_id": 1})
    tg_client.send_voice = AsyncMock(return_value={"message_id": 2})

    svc = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        telegram_client=tg_client,
    )

    with (
        patch(
            "app.services.digest_service.VoiceService.synthesize_speech",
            new=AsyncMock(return_value=b"fake-mp3-bytes"),
        ),
        patch.object(
            svc.groq_service,
            "chat_completion_with_fallback",
            new=AsyncMock(return_value=("Status update requested.", "mock")),
        ),
    ):
        res = await svc.generate_and_dispatch_digest(user_id, now=now_9am_ist)

    assert res["status"] == "delivered_telegram"
    assert res["voice_sent"] is True
    # Must send BOTH text and voice
    tg_client.send_message.assert_awaited_once()
    tg_client.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_voice_preference_and_help_commands():
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="u@test.com",
        prefs={},
        created_at=now,
        updated_at=now,
    )

    telegram_repo = MagicMock()
    telegram_repo.get_link_by_chat_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=111,
            is_active=True,
            linked_at=now,
            updated_at=now,
        )
    )
    user_repo = MagicMock()
    user_repo.get_by_id = AsyncMock(return_value=user)
    user_repo.update_user = AsyncMock(return_value=user)

    tg_client = MagicMock()
    tg_client.send_message = AsyncMock(return_value={"message_id": 1})

    svc = TelegramService(
        telegram_repo=telegram_repo,
        user_repo=user_repo,
        email_repo=MagicMock(),
        draft_repo=MagicMock(),
        approval_service=MagicMock(),
        notification_service=MagicMock(),
        telegram_client=tg_client,
    )

    await svc._handle_command(111, "/voice prabhat")
    user_repo.update_user.assert_awaited_once()
    update_arg = user_repo.update_user.call_args[0][1]
    assert update_arg.prefs["voice_name"] == "prabhat"

    await svc._handle_command(111, "/help")
    help_text = tg_client.send_message.call_args_list[-1].kwargs["text"]
    for cmd_name in ("/radar", "/unsubscribe", "/find", "/voice", "/followups"):
        assert cmd_name in help_text
