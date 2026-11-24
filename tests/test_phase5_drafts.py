"""Phase 5 tests: Draft generator, approval state machine, revision loop, and sending execution."""

import base64
import email
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import ConflictError, ValidationError
from app.main import app
from app.models.db import (
    DraftRow,
    DraftStatus,
    DraftVersionRow,
    EmailAnalysisRow,
    EmailCategory,
    EmailPriority,
    EmailRow,
    UserRow,
)
from app.services.approval_service import ApprovalService
from app.services.mime_builder import build_mime_reply


# =============================================================================
# MIME Builder Tests
# =============================================================================
def test_build_mime_reply_headers():
    raw_b64 = build_mime_reply(
        from_addr="Alex User <alex@example.com>",
        to_addr="Sam Client <sam@client.com>",
        subject="Project Update",
        body="Here is the project update text.",
        in_reply_to="<msg-123@client.com>",
        references="<msg-123@client.com>",
        idempotency_key="draft_test_123",
    )

    # Decode base64url
    decoded_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    msg = email.message_from_bytes(decoded_bytes)

    assert msg["From"] == "Alex User <alex@example.com>"
    assert msg["To"] == "Sam Client <sam@client.com>"
    assert msg["Subject"] == "Re: Project Update"
    assert msg["In-Reply-To"] == "<msg-123@client.com>"
    assert msg["References"] == "<msg-123@client.com>"
    assert msg["X-Idempotency-Key"] == "draft_test_123"
    assert "Here is the project update text." in msg.get_payload()


def test_build_mime_reply_subject_already_re():
    raw_b64 = build_mime_reply(
        from_addr="alex@example.com",
        to_addr="sam@client.com",
        subject="Re: Project Update",
        body="Response",
    )
    decoded_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    msg = email.message_from_bytes(decoded_bytes)
    assert msg["Subject"] == "Re: Project Update"


def test_build_mime_composed_email_no_re_prefix():
    raw_b64 = build_mime_reply(
        from_addr="alex@example.com",
        to_addr="sam@client.com",
        subject="Quarterly Roadmap Update",
        body="Here is our roadmap for Q3.",
        is_reply=False,
    )
    decoded_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    msg = email.message_from_bytes(decoded_bytes)
    assert msg["Subject"] == "Quarterly Roadmap Update"
    assert "In-Reply-To" not in msg
    assert "References" not in msg
    assert "Here is our roadmap for Q3." in msg.get_payload()


# =============================================================================
# State Machine & Approval Service Tests
# =============================================================================
@pytest.fixture
def mock_repos():
    draft_repo = MagicMock()
    email_repo = MagicMock()
    user_repo = MagicMock()
    auth_service = MagicMock()
    return draft_repo, email_repo, user_repo, auth_service


@pytest.mark.asyncio
async def test_invalid_transitions_raise_conflict(mock_repos):
    draft_repo, email_repo, user_repo, auth_service = mock_repos
    service = ApprovalService(draft_repo, email_repo, user_repo, auth_service)

    now = datetime.now(UTC)
    draft = DraftRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        email_id=uuid.uuid4(),
        status=DraftStatus.SENT,
        current_version=1,
        idempotency_key="test_key",
        expires_at=now,
        created_at=now,
        updated_at=now,
    )

    # SENT cannot transition to APPROVED
    with pytest.raises(ConflictError) as exc_info:
        await service.transition(draft, DraftStatus.APPROVED)
    assert "Invalid draft transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_double_approval_cas_conflict(mock_repos):
    draft_repo, email_repo, user_repo, auth_service = mock_repos
    draft_repo.transition_draft = AsyncMock(
        side_effect=ConflictError(
            "Draft cannot transition. Current draft status does not match expected state."
        )
    )

    service = ApprovalService(draft_repo, email_repo, user_repo, auth_service)
    now = datetime.now(UTC)
    draft = DraftRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        email_id=uuid.uuid4(),
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="test_key",
        expires_at=now,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ConflictError) as exc_info:
        await service.transition(draft, DraftStatus.APPROVED)
    assert "Current draft status does not match" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_draft_blocked_on_risk_flags(mock_repos):
    draft_repo, email_repo, user_repo, auth_service = mock_repos
    email_id = uuid.uuid4()
    user_id = uuid.uuid4()
    now = datetime.now(UTC)

    email_repo.get_by_id = AsyncMock(
        return_value=EmailRow(
            id=email_id,
            user_id=user_id,
            gmail_msg_id="msg_safe",
            thread_id="th_safe",
            sender="Attacker <bad@evil.com>",
            sender_email="bad@evil.com",
            subject="Urgent action required",
            body_clean="Click this phishing link",
            received_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    email_repo.get_analysis = AsyncMock(
        return_value=EmailAnalysisRow(
            email_id=email_id,
            category=EmailCategory.SECURITY,
            needs_reply=True,
            priority=EmailPriority.HIGH,
            summary="Suspicious phishing email",
            confidence=0.99,
            risk_flags=["phishing_attempt"],
            model="test-model",
            prompt_version="1.0",
            created_at=now,
        )
    )

    service = ApprovalService(draft_repo, email_repo, user_repo, auth_service)

    with pytest.raises(ValidationError) as exc:
        await service.generate_draft_for_email(email_id, user_id)
    assert "security risk flags" in str(exc.value)


@pytest.mark.asyncio
async def test_approve_and_send_success(mock_repos):
    draft_repo, email_repo, user_repo, auth_service = mock_repos
    draft_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email_id = uuid.uuid4()
    now = datetime.now(UTC)

    initial_draft = DraftRow(
        id=draft_id,
        user_id=user_id,
        email_id=email_id,
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="idemp_12345",
        expires_at=now,
        created_at=now,
        updated_at=now,
    )
    approved_draft = initial_draft.model_copy(update={"status": DraftStatus.APPROVED})
    sending_draft = initial_draft.model_copy(update={"status": DraftStatus.SENDING})
    sent_draft = initial_draft.model_copy(
        update={"status": DraftStatus.SENT, "sent_gmail_msg_id": "gmail_sent_999"}
    )

    draft_repo.get_by_id = AsyncMock(return_value=initial_draft)
    draft_repo.get_versions = AsyncMock(
        return_value=[
            DraftVersionRow(
                id=uuid.uuid4(),
                draft_id=draft_id,
                version=1,
                body="Thank you for your email. I would be delighted to attend.",
                source="llm",
                model="llama-3.3-70b-versatile",
                created_at=now,
            )
        ]
    )

    status_map = {
        "approved": approved_draft,
        "sending": sending_draft,
        "sent": sent_draft,
    }

    async def mock_transition(*_args, **kwargs):
        to_status = kwargs.get("to_status")
        return status_map.get(to_status, initial_draft)

    draft_repo.transition_draft = AsyncMock(side_effect=mock_transition)

    email_repo.get_by_id = AsyncMock(
        return_value=EmailRow(
            id=email_id,
            user_id=user_id,
            gmail_msg_id="inbox_123",
            thread_id="thread_123",
            rfc_message_id="<inbox-123@sender.com>",
            sender="Partner <partner@example.com>",
            sender_email="partner@example.com",
            subject="Partnership Discussion",
            body_clean="Can we meet next week?",
            received_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    user_repo.get_by_id = AsyncMock(
        return_value=UserRow(
            id=user_id,
            google_sub="sub_123",
            email="me@example.com",
            name="Me",
            created_at=now,
            updated_at=now,
        )
    )
    auth_service.get_valid_access_token = AsyncMock(return_value="mock_access_token")

    service = ApprovalService(draft_repo, email_repo, user_repo, auth_service)

    with patch("app.services.approval_service.GmailClient") as mock_gmail_cls:
        mock_gmail = MagicMock()
        mock_gmail.send_raw_message = AsyncMock(return_value={"id": "gmail_sent_999"})
        mock_gmail_cls.return_value = mock_gmail

        final_draft = await service.approve_and_send(draft_id, user_id)

        assert final_draft.status == DraftStatus.SENT
        assert final_draft.sent_gmail_msg_id == "gmail_sent_999"
        mock_gmail.send_raw_message.assert_awaited_once()


# =============================================================================
# API Endpoints Integration Tests
# =============================================================================
@pytest.fixture
def auth_user():
    now = datetime.now(UTC)
    return UserRow(
        id=uuid.uuid4(),
        google_sub="sub_test_user",
        email="testuser@example.com",
        name="Test User",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_drafts_api_flow(auth_user):
    user_id = auth_user.id
    draft_id = uuid.uuid4()
    email_id = uuid.uuid4()
    now = datetime.now(UTC)

    mock_draft = DraftRow(
        id=draft_id,
        user_id=user_id,
        email_id=email_id,
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="idemp_api_test",
        expires_at=now,
        created_at=now,
        updated_at=now,
    )

    mock_version = DraftVersionRow(
        id=uuid.uuid4(),
        draft_id=draft_id,
        version=1,
        body="Hello, thanks for reaching out.",
        source="llm",
        model="llama-3.3-70b-versatile",
        created_at=now,
    )

    mock_repo = MagicMock()
    mock_repo.list_by_user = AsyncMock(return_value=[mock_draft])
    mock_repo.get_by_id = AsyncMock(return_value=mock_draft)
    mock_repo.get_versions = AsyncMock(return_value=[mock_version])

    mock_svc = MagicMock()
    mock_svc.approve_and_send = AsyncMock(
        return_value=mock_draft.model_copy(update={"status": DraftStatus.SENT})
    )
    mock_svc.reject_draft = AsyncMock(
        return_value=mock_draft.model_copy(update={"status": DraftStatus.REJECTED})
    )
    mock_version_2 = DraftVersionRow(
        id=uuid.uuid4(),
        draft_id=draft_id,
        version=2,
        body="Revised concise reply.",
        source="revision",
        model="llama-3.3-70b-versatile",
        created_at=now,
    )
    mock_svc.revise_draft = AsyncMock(
        return_value=(
            mock_draft.model_copy(update={"current_version": 2}),
            mock_version_2,
        )
    )
    mock_svc.edit_draft_manually = AsyncMock(
        return_value=(
            mock_draft.model_copy(update={"current_version": 2}),
            mock_version_2,
        )
    )

    from app.api.deps import get_current_user
    from app.api.v1.drafts import get_approval_service, get_draft_repo

    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_draft_repo] = lambda: mock_repo
    app.dependency_overrides[get_approval_service] = lambda: mock_svc

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 1. List drafts
            resp = await client.get("/api/v1/drafts")
            assert resp.status_code == 200
            items = resp.json()
            assert len(items) == 1
            assert items[0]["id"] == str(draft_id)

            # 2. Get draft detail
            resp = await client.get(f"/api/v1/drafts/{draft_id}")
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["draft"]["id"] == str(draft_id)
            assert len(detail["versions"]) == 1

            # 3. Reject draft
            resp = await client.post(
                f"/api/v1/drafts/{draft_id}/reject",
                headers={"Authorization": "Bearer programmatic_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"

            # 4. Revise draft
            mock_repo.get_versions = AsyncMock(return_value=[mock_version, mock_version_2])
            resp = await client.post(
                f"/api/v1/drafts/{draft_id}/revise",
                json={"instruction": "Make it shorter", "tone": "shorter"},
                headers={"Authorization": "Bearer programmatic_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["draft"]["current_version"] == 2

            # 5. Approve draft
            resp = await client.post(
                f"/api/v1/drafts/{draft_id}/approve",
                headers={"Authorization": "Bearer programmatic_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "sent"
    finally:
        app.dependency_overrides.clear()


async def test_create_custom_draft_success(mock_repos):
    mock_draft_repo, mock_email_repo, mock_user_repo, mock_auth_service = mock_repos
    service = ApprovalService(mock_draft_repo, mock_email_repo, mock_user_repo, mock_auth_service)
    user_id = uuid.uuid4()
    mock_user = UserRow(
        id=user_id,
        email="alex@example.com",
        name="Alex",
        google_sub="sub_123",
        avatar_url=None,
        google_access_token="tok",
        google_refresh_token="ref",
        google_token_expires_at=datetime.now(UTC),
        telegram_chat_id=123,
        prefs={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_user_repo.get_by_id = AsyncMock(return_value=mock_user)
    mock_auth_service.get_user_groq_api_key = AsyncMock(return_value=None)
    mock_created_email = EmailRow(
        id=uuid.uuid4(),
        user_id=user_id,
        gmail_msg_id="outbound_123",
        thread_id="th_123",
        rfc_message_id=None,
        sender="Alex <alex@example.com>",
        sender_email="sam@example.com",
        reply_to="sam@example.com",
        subject="Project Status Update",
        received_at=datetime.now(UTC),
        snippet="Hi Sam",
        body_clean="Tell him the project is on track",
        has_attachments=False,
        labels=["COMPOSED", "OUTBOUND"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_email_repo.upsert_email = AsyncMock(return_value=mock_created_email)
    created_draft = DraftRow(
        id=uuid.uuid4(),
        user_id=user_id,
        email_id=mock_created_email.id,
        status=DraftStatus.DRAFT_GENERATED,
        current_version=1,
        idempotency_key="idemp_1",
        expires_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    pending_draft = DraftRow(
        id=created_draft.id,
        user_id=user_id,
        email_id=mock_created_email.id,
        status=DraftStatus.PENDING_APPROVAL,
        current_version=1,
        idempotency_key="idemp_1",
        expires_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_draft_repo.create_draft = AsyncMock(return_value=created_draft)
    mock_draft_repo.transition_draft = AsyncMock(return_value=pending_draft)
    mock_draft_repo.create_version = AsyncMock(return_value=MagicMock())

    mock_groq_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = (
        '{"subject": "Project Status Update", "body": "Hi Sam,\\nUpdate.\\nBest,"}'
    )
    mock_groq_resp.choices = [mock_choice]

    with patch.object(
        service.groq_client.chat.completions,
        "create",
        AsyncMock(return_value=mock_groq_resp),
    ):
        draft, email_record = await service.create_custom_draft(
            user_id=user_id,
            recipient="sam@example.com",
            user_instruction="Tell him the project is on track",
        )

        assert email_record.sender_email == "sam@example.com"
        assert "COMPOSED" in email_record.labels
        assert "OUTBOUND" in email_record.labels
        assert draft.status == DraftStatus.PENDING_APPROVAL
        assert mock_draft_repo.create_draft.called
        assert mock_draft_repo.create_version.called
