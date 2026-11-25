"""Phase 7 tests: ARQ worker, cron scheduler, deterministic jobs, and daily digest service."""

import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.db import (
    DraftRow,
    DraftStatus,
    DraftVersionRow,
    EmailAnalysisCreate,
    EmailAnalysisRow,
    EmailRow,
    NotificationPrefsRow,
    TelegramLinkRow,
    UserRow,
)
from app.queue.worker import (
    run_user_sync_pipeline,
    scheduler_cron_job,
)
from app.services.digest_service import DigestService, compute_next_digest_time


# =============================================================================
# Timezone & Schedule Calculation Tests
# =============================================================================
def test_compute_next_digest_time_same_day():
    # Freeze time to 06:00 UTC, target 08:00 UTC
    freeze_now = datetime(2026, 6, 15, 6, 0, 0, tzinfo=UTC)
    target_time = time(8, 0)

    next_time = compute_next_digest_time(target_time, "UTC", now=freeze_now)

    assert next_time == datetime(2026, 6, 15, 8, 0, 0, tzinfo=UTC)


def test_compute_next_digest_time_next_day():
    # Freeze time to 10:00 UTC, target 08:00 UTC -> should advance to tomorrow 08:00
    freeze_now = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
    target_time = time(8, 0)

    next_time = compute_next_digest_time(target_time, "UTC", now=freeze_now)

    assert next_time == datetime(2026, 6, 16, 8, 0, 0, tzinfo=UTC)


def test_compute_next_digest_time_cross_timezone():
    # America/New_York (UTC-4 in summer DST). 09:00 NY = 13:00 UTC.
    freeze_now = datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC)  # 07:00 NY
    target_time = time(9, 0)

    next_time = compute_next_digest_time(target_time, "America/New_York", now=freeze_now)

    # In June, NY is EDT (UTC-4), so 09:00 EDT = 13:00 UTC on the same day
    assert next_time == datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC)


# =============================================================================
# DigestService Tests
# =============================================================================
@pytest.fixture
def digest_repos():
    user_repo = MagicMock()
    email_repo = MagicMock()
    draft_repo = MagicMock()
    telegram_repo = MagicMock()
    client = MagicMock()
    client.send_message = AsyncMock(return_value={"message_id": 10})
    return user_repo, email_repo, draft_repo, telegram_repo, client


@pytest.mark.asyncio
async def test_digest_skipped_if_disabled_in_prefs(digest_repos):
    user_repo, email_repo, draft_repo, telegram_repo, client = digest_repos
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="user@test.com",
        created_at=now,
        updated_at=now,
    )
    prefs = NotificationPrefsRow(
        user_id=user_id,
        channel="telegram",
        digest_enabled=False,
        created_at=now,
        updated_at=now,
    )

    user_repo.get_by_id = AsyncMock(return_value=user)
    user_repo.get_notification_prefs = AsyncMock(return_value=prefs)

    service = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        telegram_client=client,
    )

    res = await service.generate_and_dispatch_digest(user_id)
    assert res["status"] == "skipped"
    assert res["reason"] == "digest_disabled_in_prefs"
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_digest_dispatched_via_telegram(digest_repos):
    user_repo, email_repo, draft_repo, telegram_repo, client = digest_repos
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="user@test.com",
        name="Alice",
        created_at=now,
        updated_at=now,
    )
    prefs = NotificationPrefsRow(
        user_id=user_id,
        channel="telegram",
        digest_enabled=True,
        created_at=now,
        updated_at=now,
    )

    email1 = EmailRow(
        id=uuid.uuid4(),
        user_id=user_id,
        gmail_msg_id="m1",
        thread_id="t1",
        sender="Boss <boss@company.com>",
        sender_email="boss@company.com",
        subject="URGENT: Quarterly review",
        body_clean="Review numbers ASAP",
        received_at=now,
        created_at=now,
        updated_at=now,
    )

    draft1 = DraftRow(
        id=uuid.uuid4(),
        user_id=user_id,
        email_id=email1.id,
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="idemp_1",
        expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )

    user_repo.get_by_id = AsyncMock(return_value=user)
    user_repo.get_notification_prefs = AsyncMock(return_value=prefs)
    email_repo.list_by_user = AsyncMock(return_value=[email1])
    draft_repo.list_by_user = AsyncMock(return_value=[draft1])
    telegram_repo.get_link_by_user_id = AsyncMock(
        return_value=TelegramLinkRow(
            user_id=user_id,
            chat_id=555123,
            is_active=True,
            linked_at=now,
            created_at=now,
            updated_at=now,
        )
    )

    service = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        telegram_client=client,
    )

    res = await service.generate_and_dispatch_digest(user_id)
    assert res["status"] == "delivered_telegram"
    assert res["chat_id"] == 555123
    assert res["emails_summarized"] == 1
    assert res["pending_drafts"] == 1
    client.send_message.assert_awaited_once()


# =============================================================================
# ARQ Worker & Scheduler Tests
# =============================================================================
@pytest.mark.asyncio
async def test_scheduler_cron_job_enqueues_deterministic_jobs():
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    due_user = UserRow(
        id=user_id,
        google_sub="sub_due",
        email="due@example.com",
        timezone="UTC",
        digest_time=time(8, 0),
        prefs={"groq_api_key_encrypted": "enc:v1:test"},
        next_digest_at=now - timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )

    user_repo = MagicMock()
    user_repo.claim_due_users = AsyncMock(return_value=[due_user])
    user_repo.update_user = AsyncMock()

    mock_redis = MagicMock()
    mock_redis.enqueue_job = AsyncMock()

    ctx = {
        "user_repo": user_repo,
        "redis": mock_redis,
    }

    result = await scheduler_cron_job(ctx)
    assert result["claimed_users"] == 1

    # Verify user next_digest_at was advanced
    user_repo.update_user.assert_awaited_once()
    # Verify deterministic jobs were enqueued
    assert mock_redis.enqueue_job.await_count == 2
    calls = mock_redis.enqueue_job.await_args_list

    # 1. Digest job
    assert calls[0][0][0] == "run_user_digest"
    assert calls[0][1]["_job_id"].startswith(f"digest_{user_id}_")

    # 2. Sync pipeline job
    assert calls[1][0][0] == "run_user_sync_pipeline"
    assert calls[1][1]["_job_id"].startswith(f"sync_{user_id}_")


@pytest.mark.asyncio
async def test_run_user_sync_pipeline():
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    email1 = EmailRow(
        id=uuid.uuid4(),
        user_id=user_id,
        gmail_msg_id="msg_pip",
        thread_id="th_pip",
        sender="Partner <partner@example.com>",
        sender_email="partner@example.com",
        subject="Meeting request",
        body_clean="Can we meet at 3pm?",
        received_at=now,
        created_at=now,
        updated_at=now,
    )

    mock_analysis = EmailAnalysisCreate(
        email_id=str(email1.id),
        category="work",
        needs_reply=True,
        priority="medium",
        summary="Requesting a meeting at 3pm",
        confidence=0.95,
        risk_flags=[],
        model="llama",
        prompt_version="1",
    )

    mock_draft = DraftRow(
        id=uuid.uuid4(),
        user_id=user_id,
        email_id=email1.id,
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="idemp_pip",
        expires_at=now + timedelta(days=2),
        created_at=now,
        updated_at=now,
    )

    mock_version = DraftVersionRow(
        id=uuid.uuid4(),
        draft_id=mock_draft.id,
        version=1,
        body="Yes, 3pm works.",
        source="llm",
        model="llama-3.3-70b-versatile",
        created_at=now,
    )

    sync_svc = MagicMock()
    sync_svc.sync_user_emails = AsyncMock(return_value={"new_emails_synced": 1})

    email_repo = MagicMock()
    email_repo.list_by_user = AsyncMock(return_value=[email1])
    email_repo.get_by_id = AsyncMock(return_value=email1)
    email_repo.get_analysis = AsyncMock(return_value=None)
    email_repo.upsert_analysis = AsyncMock(
        return_value=EmailAnalysisRow.model_validate(
            {**mock_analysis.model_dump(), "created_at": now.isoformat()}
        )
    )

    groq_svc = MagicMock()
    groq_svc.analyze_email = AsyncMock(return_value=mock_analysis)

    approval_svc = MagicMock()
    approval_svc.generate_draft_for_email = AsyncMock(return_value=mock_draft)

    draft_repo = MagicMock()
    draft_repo.get_versions = AsyncMock(return_value=[mock_version])

    notif_svc = MagicMock()
    notif_svc.notify_new_draft = AsyncMock(return_value={"status": "delivered"})

    ctx = {
        "sync_service": sync_svc,
        "email_repo": email_repo,
        "groq_service": groq_svc,
        "approval_service": approval_svc,
        "notification_service": notif_svc,
        "draft_repo": draft_repo,
    }

    res = await run_user_sync_pipeline(ctx, str(user_id))

    assert res["new_emails_synced"] == 1
    assert res["emails_analyzed"] == 1
    assert res["drafts_created"] == 1
    approval_svc.generate_draft_for_email.assert_awaited_once_with(email1.id, str(user_id))
    notif_svc.notify_new_draft.assert_awaited_once()


# =============================================================================
# Cron API Endpoint Tests
# =============================================================================
@pytest.mark.asyncio
async def test_cron_trigger_endpoint_unauthorized(client):
    """Calling /api/v1/cron/trigger without auth raises 403."""
    res = await client.get("/api/v1/cron/trigger")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_cron_trigger_endpoint_authorized(client, test_settings):
    """Calling /api/v1/cron/trigger with valid secret succeeds."""
    with patch(
        "app.api.v1.cron.run_direct_sync_cycle",
        new_callable=AsyncMock,
        return_value={"status": "success", "claimed_users": 0, "results": []},
    ):
        secret = test_settings.APP_SECRET_KEY.get_secret_value()
        res = await client.get(f"/api/v1/cron/trigger?secret={secret}")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["status"] in ("accepted", "success")
