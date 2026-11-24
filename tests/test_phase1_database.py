"""Tests for Phase 1: Database models, Repository layer, PostgREST error mapping, RLS isolation."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.repos.base import BaseRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import EmailListItem, EmailRow, UserRow


def test_user_row_model_validation() -> None:
    """Validate UserRow model from raw database dict."""
    user_id = uuid4()
    now = datetime.now(UTC)
    raw_data = {
        "id": str(user_id),
        "google_sub": "sub-12345",
        "email": "test@example.com",
        "name": "Test User",
        "timezone": "UTC",
        "digest_time": "09:00:00",
        "locale": "en",
        "prefs": {"tone": "friendly"},
        "feature_flags": {"smart_actions": True},
        "next_digest_at": now.isoformat(),
        "deleted_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    user = UserRow.model_validate(raw_data)
    assert user.id == user_id
    assert user.email == "test@example.com"
    assert user.feature_flags["smart_actions"] is True


def test_email_list_item_excludes_body_clean() -> None:
    """Ensure EmailListItem projection excludes body_clean."""
    assert "body_clean" not in EmailListItem.model_fields
    assert "body_clean" in EmailRow.model_fields


@pytest.mark.asyncio
async def test_base_repo_error_mapping_not_found() -> None:
    """Ensure PGRST116 (0 rows) maps to domain NotFoundError."""
    mock_client = MagicMock()
    repo = BaseRepo(mock_client)

    api_err = APIError(
        {
            "code": "PGRST116",
            "message": "JSON object requested, multiple (or no) rows returned",
            "details": None,
            "hint": None,
        }
    )

    async def _failing_op() -> None:
        raise api_err

    with pytest.raises(NotFoundError):
        await repo._execute_with_retry(_failing_op, max_retries=1)


@pytest.mark.asyncio
async def test_base_repo_error_mapping_unique_violation() -> None:
    """Ensure Postgres 23505 duplicate key maps to ConflictError."""
    mock_client = MagicMock()
    repo = BaseRepo(mock_client)

    api_err = APIError(
        {
            "code": "23505",
            "message": "duplicate key value violates unique constraint",
            "details": None,
            "hint": None,
        }
    )

    async def _failing_op() -> None:
        raise api_err

    with pytest.raises(ConflictError):
        await repo._execute_with_retry(_failing_op, max_retries=1)


@pytest.mark.asyncio
async def test_base_repo_error_mapping_foreign_key_violation() -> None:
    """Ensure Postgres 23503 foreign key violation maps to ValidationError."""
    mock_client = MagicMock()
    repo = BaseRepo(mock_client)

    api_err = APIError(
        {
            "code": "23503",
            "message": "violates foreign key constraint",
            "details": None,
            "hint": None,
        }
    )

    async def _failing_op() -> None:
        raise api_err

    with pytest.raises(ValidationError):
        await repo._execute_with_retry(_failing_op, max_retries=1)


@pytest.mark.asyncio
async def test_single_use_link_token_consumption() -> None:
    """Test that link token consumption is strictly single-use."""
    mock_client = MagicMock()
    repo = TelegramRepo(mock_client)

    test_uid = uuid4()
    # First invocation returns 1 row (success)
    mock_response_success = MagicMock(data=[{"user_id": str(test_uid)}])
    # Second invocation returns 0 rows (already consumed / expired)
    mock_response_fail = MagicMock(data=[])

    mock_query_builder = MagicMock()
    mock_query_builder.update.return_value = mock_query_builder
    mock_query_builder.eq.return_value = mock_query_builder
    mock_query_builder.is_.return_value = mock_query_builder
    mock_query_builder.gt.return_value = mock_query_builder

    mock_client.table.return_value = mock_query_builder

    # 1st call -> succeeds
    mock_query_builder.execute = AsyncMock(return_value=mock_response_success)
    consumed_uid = await repo.consume_link_token("hashed_token_abc")
    assert consumed_uid == test_uid

    # 2nd call -> returns None
    mock_query_builder.execute = AsyncMock(return_value=mock_response_fail)
    consumed_second = await repo.consume_link_token("hashed_token_abc")
    assert consumed_second is None


@pytest.mark.asyncio
async def test_rls_anon_key_rejection_simulation() -> None:
    """Test that calls using anon role key simulate RLS default deny error.

    Demonstrates that without secret key permissions, PostgREST returns permission denied.
    """
    mock_client = MagicMock()
    repo = UserRepo(mock_client)

    anon_denied_err = APIError(
        {
            "code": "42501",
            "message": "permission denied for table users",
            "details": None,
            "hint": None,
        }
    )

    mock_query_builder = MagicMock()
    mock_query_builder.select.return_value = mock_query_builder
    mock_query_builder.eq.return_value = mock_query_builder
    mock_query_builder.execute = AsyncMock(side_effect=anon_denied_err)
    mock_client.table.return_value = mock_query_builder

    with pytest.raises(ValidationError) as exc_info:
        await repo.get_by_id(uuid4())
    assert "permission denied" in str(exc_info.value.message).lower()
