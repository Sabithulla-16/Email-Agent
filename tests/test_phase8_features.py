"""Phase 8 tests: Rules engine, smart actions, commitments, Q&A citations, and extractors."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.db import EmailRow, RuleRow, UserRow
from app.services.extra_extractors import ExtraExtractorsService
from app.services.followup_service import FollowupService
from app.services.inbox_qa import InboxQAService
from app.services.rules_engine import RulesEngine
from app.services.smart_actions import SmartActionsService


# =============================================================================
# Feature Flag Enforcement Tests
# =============================================================================
@pytest.mark.asyncio
async def test_feature_flag_disabled_raises_forbidden():
    now = datetime.now(UTC)
    disabled_user = UserRow(
        id=uuid.uuid4(),
        google_sub="sub_flag",
        email="flag@example.com",
        feature_flags={"rules_engine": False},
        created_at=now,
        updated_at=now,
    )

    from app.api.deps import get_current_user, get_rule_repo

    app.dependency_overrides[get_current_user] = lambda: disabled_user
    app.dependency_overrides[get_rule_repo] = lambda: MagicMock()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.get("/api/v1/features/rules")
            assert resp.status_code == 403
            assert "is disabled for your account" in resp.json()["message"]
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# Rules Engine Tests
# =============================================================================
@pytest.mark.asyncio
async def test_rules_engine_trust_ladder():
    now = datetime.now(UTC)
    rule_repo = MagicMock()
    user_id = uuid.uuid4()

    rule_suggest = RuleRow(
        id=uuid.uuid4(),
        user_id=user_id,
        condition={"sender_contains": "newsletter.com"},
        action={"label": "Newsletters"},
        trust_level="suggest",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    rule_auto = RuleRow(
        id=uuid.uuid4(),
        user_id=user_id,
        condition={"subject_contains": "Receipt"},
        action={"label": "Receipts"},
        trust_level="auto",
        enabled=True,
        created_at=now,
        updated_at=now,
    )

    rule_repo.list_by_user = AsyncMock(return_value=[rule_suggest, rule_auto])
    engine = RulesEngine(rule_repo)

    test_email = EmailRow(
        id=uuid.uuid4(),
        user_id=user_id,
        gmail_msg_id="m1",
        thread_id="t1",
        sender="Store <billing@newsletter.com>",
        sender_email="billing@newsletter.com",
        subject="Your Purchase Receipt",
        body_clean="Thank you for your order.",
        received_at=now,
        created_at=now,
        updated_at=now,
    )

    results = await engine.evaluate_email(user_id, test_email)

    assert len(results) == 2
    executions = {r["execution"] for r in results}
    assert "suggest_only" in executions
    assert "auto_silent" in executions


# =============================================================================
# Smart Actions & Calendar URL Tests
# =============================================================================
def test_smart_actions_google_calendar_url():
    service = SmartActionsService()
    start = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)
    url = service.generate_google_calendar_url(
        title="Strategy Sync",
        start_time=start,
        duration_minutes=45,
        details="Important sync",
    )
    assert url.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert "Strategy+Sync" in url
    assert "20260710T150000Z%2F20260710T154500Z" in url


def test_smart_actions_extract_meeting():
    now = datetime.now(UTC)
    email = EmailRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        gmail_msg_id="m_meet",
        thread_id="t_meet",
        sender="Colleague <colleague@work.com>",
        sender_email="colleague@work.com",
        subject="Sync about Q3 Roadmap",
        body_clean="Can we schedule a quick Zoom call to catch up?",
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    service = SmartActionsService()
    res = service.extract_meeting_details(email)

    assert res is not None
    assert res["has_meeting"] is True
    assert "calendar_url" in res


# =============================================================================
# Followup & Commitment Tracker Tests
# =============================================================================
def test_detect_commitments_from_body():
    service = FollowupService()
    text = "Thanks for the feedback. I will send the revised slides tomorrow morning."
    commitments = service.detect_commitments(text)

    assert len(commitments) == 1
    assert "send the revised slides" in commitments[0]


# =============================================================================
# Extra Extractors Tests (Bills, Travel, Phishing)
# =============================================================================
def test_extra_extractors_bill():
    now = datetime.now(UTC)
    email = EmailRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        gmail_msg_id="m_bill",
        thread_id="t_bill",
        sender="Acme Cloud <billing@acme.com>",
        sender_email="billing@acme.com",
        subject="Your Monthly Invoice",
        body_clean="Total amount due: $149.50. Thank you for your business.",
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    service = ExtraExtractorsService()
    bill = service.extract_bill(email)

    assert bill is not None
    assert bill["has_bill"] is True
    assert bill["amount"] == 149.50
    assert bill["currency"] == "USD"


def test_extra_extractors_travel():
    now = datetime.now(UTC)
    email = EmailRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        gmail_msg_id="m_travel",
        thread_id="t_travel",
        sender="Airline <tickets@airline.com>",
        sender_email="tickets@airline.com",
        subject="Flight Confirmation: SFO to JFK",
        body_clean="Your reservation is confirmed. Booking Reference: KLM789X.",
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    service = ExtraExtractorsService()
    travel = service.extract_travel_reservation(email)

    assert travel is not None
    assert travel["has_travel"] is True
    assert travel["booking_reference"] == "KLM789X"


def test_extra_extractors_phishing_detection():
    now = datetime.now(UTC)
    phishing_email = EmailRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        gmail_msg_id="m_phish",
        thread_id="t_phish",
        sender="PayPal Security <support@paypa1-verify.com>",
        sender_email="support@paypa1-verify.com",
        subject="Account Suspended - Urgent Action Required",
        body_clean="Your account suspended. Click here to verify your password immediately.",
        received_at=now,
        created_at=now,
        updated_at=now,
    )
    service = ExtraExtractorsService()
    warnings = service.scan_phishing_indicators(phishing_email)

    assert len(warnings) >= 2
    assert any("threat or credential" in w for w in warnings)
    assert any("does not match sender domain" in w for w in warnings)


# =============================================================================
# Inbox Q&A Tests
# =============================================================================
@pytest.mark.asyncio
async def test_inbox_qa_grounded_answer():
    now = datetime.now(UTC)
    user_id = uuid.uuid4()
    email_repo = MagicMock()

    email1 = EmailRow(
        id=uuid.uuid4(),
        user_id=user_id,
        gmail_msg_id="m_qa",
        thread_id="t_qa",
        sender="CFO <cfo@company.com>",
        sender_email="cfo@company.com",
        subject="Q3 Budget Approval",
        body_clean="The approved budget for the engineering team is $450,000.",
        received_at=now,
        created_at=now,
        updated_at=now,
    )

    email_repo.list_by_user = AsyncMock(return_value=[email1])
    email_repo.get_by_id = AsyncMock(return_value=email1)

    service = InboxQAService(email_repo=email_repo)

    mock_chat_resp = MagicMock()
    mock_chat_resp.choices = [
        MagicMock(
            message=MagicMock(
                content="The approved engineering budget is $450,000 as stated in [Email #1]."
            )
        )
    ]
    service.groq_client.chat.completions.create = AsyncMock(return_value=mock_chat_resp)

    res = await service.answer_question(user_id=user_id, query="What is the engineering budget?")

    assert "$450,000" in res["answer"]
    assert len(res["citations"]) == 1
    assert res["citations"][0]["ref"] == "Email #1"
