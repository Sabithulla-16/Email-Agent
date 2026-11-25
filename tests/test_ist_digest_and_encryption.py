"""Tests for IST 3x daily incremental digest schedule and Supabase field-level encryption."""

import uuid
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.vault import ENC_PREFIX, TokenVault
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    EmailAnalysisCreate,
    EmailCreate,
    NotificationPrefsRow,
    UserCreate,
    UserRow,
)
from app.services.digest_service import (
    DEFAULT_DIGEST_SLOTS,
    DigestService,
    compute_next_digest_time,
    get_digest_window_and_label,
)


# =============================================================================
# IST 3x Daily Digest Schedule Tests (9 AM, 3 PM, 9 PM IST)
# =============================================================================
def test_compute_next_digest_slots_ist():
    # 08:00 IST = 02:30 UTC -> next slot is 09:00 IST = 03:30 UTC same day
    now_0800_ist = datetime(2026, 11, 25, 2, 30, 0, tzinfo=UTC)
    assert compute_next_digest_time(
        time(9, 0), "Asia/Kolkata", now=now_0800_ist, digest_slots=DEFAULT_DIGEST_SLOTS
    ) == datetime(2026, 11, 25, 3, 30, 0, tzinfo=UTC)

    # 09:05 IST = 03:35 UTC -> next slot is 15:00 IST (3 PM) = 09:30 UTC same day
    now_0905_ist = datetime(2026, 11, 25, 3, 35, 0, tzinfo=UTC)
    assert compute_next_digest_time(
        time(9, 0), "Asia/Kolkata", now=now_0905_ist, digest_slots=DEFAULT_DIGEST_SLOTS
    ) == datetime(2026, 11, 25, 9, 30, 0, tzinfo=UTC)

    # 15:10 IST = 09:40 UTC -> next slot is 21:00 IST (9 PM) = 15:30 UTC same day
    now_1510_ist = datetime(2026, 11, 25, 9, 40, 0, tzinfo=UTC)
    assert compute_next_digest_time(
        time(9, 0), "Asia/Kolkata", now=now_1510_ist, digest_slots=DEFAULT_DIGEST_SLOTS
    ) == datetime(2026, 11, 25, 15, 30, 0, tzinfo=UTC)

    # 21:05 IST = 15:35 UTC -> next slot is 09:00 IST next day = 03:30 UTC Nov 26
    now_2105_ist = datetime(2026, 11, 25, 15, 35, 0, tzinfo=UTC)
    assert compute_next_digest_time(
        time(9, 0), "Asia/Kolkata", now=now_2105_ist, digest_slots=DEFAULT_DIGEST_SLOTS
    ) == datetime(2026, 11, 26, 3, 30, 0, tzinfo=UTC)


def test_digest_window_and_label_ist():
    # Morning 9:00 AM IST (03:30 UTC) -> previous 9 PM IST (Nov 24 15:30 UTC)
    morning_utc = datetime(2026, 11, 25, 3, 30, 0, tzinfo=UTC)
    since, title, is_inc = get_digest_window_and_label("Asia/Kolkata", now=morning_utc)
    assert since == datetime(2026, 11, 24, 15, 30, 0, tzinfo=UTC)
    assert "Morning Digest" in title
    assert is_inc is False

    # Afternoon 3:00 PM IST (09:30 UTC) -> uses last_digest_at if recent
    afternoon_utc = datetime(2026, 11, 25, 9, 30, 0, tzinfo=UTC)
    last_digest = datetime(2026, 11, 25, 3, 32, 0, tzinfo=UTC)
    since, title, is_inc = get_digest_window_and_label(
        "Asia/Kolkata", now=afternoon_utc, last_digest_at=last_digest
    )
    assert since == last_digest
    assert "Afternoon Update" in title
    assert is_inc is True

    # Evening 9:00 PM IST (15:30 UTC)
    evening_utc = datetime(2026, 11, 25, 15, 30, 0, tzinfo=UTC)
    since, title, is_inc = get_digest_window_and_label("Asia/Kolkata", now=evening_utc)
    assert since == datetime(2026, 11, 25, 9, 30, 0, tzinfo=UTC)
    assert "Evening Update" in title
    assert is_inc is True


@pytest.mark.asyncio
async def test_incremental_digest_skips_when_no_new_emails():
    user_id = uuid.uuid4()
    now_3pm_ist = datetime(2026, 11, 25, 9, 30, 0, tzinfo=UTC)
    user = UserRow(
        id=user_id,
        google_sub="sub1",
        email="u@test.com",
        timezone="Asia/Kolkata",
        created_at=now_3pm_ist,
        updated_at=now_3pm_ist,
    )
    prefs = NotificationPrefsRow(
        user_id=user_id,
        channel="telegram",
        digest_enabled=True,
        created_at=now_3pm_ist,
        updated_at=now_3pm_ist,
    )

    user_repo = MagicMock()
    user_repo.get_by_id = AsyncMock(return_value=user)
    user_repo.get_notification_prefs = AsyncMock(return_value=prefs)
    user_repo.update_user = AsyncMock(return_value=user)

    email_repo = MagicMock()
    email_repo.list_by_user_since = AsyncMock(return_value=[])

    draft_repo = MagicMock()
    telegram_repo = MagicMock()
    tg_client = MagicMock()
    tg_client.send_message = AsyncMock()

    svc = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        telegram_client=tg_client,
    )
    res = await svc.generate_and_dispatch_digest(user_id, now=now_3pm_ist)

    assert res["status"] == "skipped"
    assert res["reason"] == "no_new_emails"
    tg_client.send_message.assert_not_called()
    user_repo.update_user.assert_awaited_once()


# =============================================================================
# Supabase Field-Level Encryption & Blind Index Tests
# =============================================================================
def test_vault_field_encryption_and_blind_index():
    vault = TokenVault()
    secret = "Confidential email body with OTP 123456"

    enc = vault.encrypt_field(secret)
    assert enc is not None
    assert enc.startswith(f"{ENC_PREFIX}{vault.active_version}:")
    assert secret not in enc

    # Idempotent: encrypting an already encrypted string does not double-encrypt
    assert vault.encrypt_field(enc) == enc

    # Round-trip decryption
    assert vault.decrypt_field(enc) == secret

    # Graceful fallback for legacy plaintext rows
    assert vault.decrypt_field("Legacy plaintext") == "Legacy plaintext"
    assert vault.decrypt_field(None) is None

    # Blind index is deterministic & case/whitespace insensitive
    idx1 = vault.blind_index("Alice@Example.com ")
    idx2 = vault.blind_index("alice@example.com")
    assert idx1 == idx2
    assert len(idx1) == 64

    # JSON string encryption/decryption
    items = [{"task": "Pay invoice", "assignee": "Bob"}]
    enc_items = vault.encrypt_json_strings(items)
    assert enc_items[0]["task"].startswith(ENC_PREFIX)
    assert vault.decrypt_json_strings(enc_items) == items


@pytest.mark.asyncio
async def test_repos_encrypt_before_db_and_decrypt_on_read():
    vault = TokenVault()
    now = datetime.now(UTC)
    user_id = uuid.uuid4()
    email_id = uuid.uuid4()
    draft_id = uuid.uuid4()

    # Mock Supabase client capturing written payloads
    stored_email_payload: dict = {}

    def _make_exec(ret_data):
        res = MagicMock()
        res.data = ret_data
        return AsyncMock(return_value=res)

    email_table = MagicMock()

    def _email_upsert(payload, **_kwargs):
        stored_email_payload.update(payload)
        row = {
            "id": str(email_id),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            **payload,
        }
        q = MagicMock()
        q.execute = _make_exec([row])
        return q

    email_table.upsert = _email_upsert

    analysis_table = MagicMock()
    stored_analysis_payload: dict = {}

    def _analysis_upsert(payload, **_kwargs):
        stored_analysis_payload.update(payload)
        row = {"created_at": now.isoformat(), **payload}
        q = MagicMock()
        q.execute = _make_exec([row])
        return q

    analysis_table.upsert = _analysis_upsert

    user_table = MagicMock()
    stored_user_payload: dict = {}

    def _user_upsert(payload, **_kwargs):
        stored_user_payload.update(payload)
        row = {
            "id": str(user_id),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            **payload,
        }
        q = MagicMock()
        q.execute = _make_exec([row])
        return q

    user_table.upsert = _user_upsert

    client = MagicMock()
    client.table = lambda name: {
        "emails": email_table,
        "email_analysis": analysis_table,
        "users": user_table,
    }[name]

    stored_rpc_params: dict = {}

    def _rpc(_fn_name, params):
        stored_rpc_params.update(params)
        q = MagicMock()
        q.execute = _make_exec(
            {
                "id": str(uuid.uuid4()),
                "draft_id": str(draft_id),
                "version": 1,
                "body": params["p_body"],
                "user_instruction": params["p_user_instruction"],
                "source": params["p_source"],
                "model": params["p_model"],
                "created_at": now.isoformat(),
            }
        )
        return q

    client.rpc = _rpc

    # 1. UserRepo encrypts email & name in DB, decrypts on return
    user_repo = UserRepo(client, vault=vault)
    u_row = await user_repo.upsert_user(
        UserCreate(google_sub="sub_1", email="secret@user.com", name="Secret Name")
    )
    assert stored_user_payload["email"].startswith(ENC_PREFIX)
    assert stored_user_payload["name"].startswith(ENC_PREFIX)
    assert u_row.email == "secret@user.com"
    assert u_row.name == "Secret Name"

    # 2. EmailRepo encrypts email & analysis in DB, decrypts on return
    email_repo = EmailRepo(client, vault=vault)
    e_row = await email_repo.upsert_email(
        EmailCreate(
            user_id=str(user_id),
            gmail_msg_id="gm_1",
            thread_id="th_1",
            sender="Boss",
            sender_email="boss@corp.com",
            subject="Confidential Merger",
            received_at=now.isoformat(),
            snippet="Top secret details",
            body_clean="Full confidential body text",
        )
    )
    assert stored_email_payload["subject"].startswith(ENC_PREFIX)
    assert stored_email_payload["body_clean"].startswith(ENC_PREFIX)
    assert stored_email_payload["sender_email"].startswith(ENC_PREFIX)
    assert e_row.subject == "Confidential Merger"
    assert e_row.body_clean == "Full confidential body text"

    a_row = await email_repo.upsert_analysis(
        EmailAnalysisCreate(
            email_id=str(email_id),
            category="work",
            needs_reply=True,
            priority="high",
            summary="Confidential summary",
            action_items=[{"task": "Sign NDA"}],
            confidence=0.95,
            model="gpt-oss-120b",
            prompt_version="v1",
        )
    )
    assert stored_analysis_payload["summary"].startswith(ENC_PREFIX)
    assert stored_analysis_payload["action_items"][0]["task"].startswith(ENC_PREFIX)
    assert a_row.summary == "Confidential summary"
    assert a_row.action_items == [{"task": "Sign NDA"}]

    # 3. DraftRepo encrypts body & instruction before RPC, decrypts on return
    draft_repo = DraftRepo(client, vault=vault)
    dv_row = await draft_repo.create_version(
        draft_id=draft_id,
        body="Confidential reply draft",
        user_instruction="Make it polite",
    )
    assert stored_rpc_params["p_body"].startswith(ENC_PREFIX)
    assert stored_rpc_params["p_user_instruction"].startswith(ENC_PREFIX)
    assert dv_row.body == "Confidential reply draft"
    assert dv_row.user_instruction == "Make it polite"
