"""Phase 9 tests: Retention cleanup, security review, and OpenAPI schema verification."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app
from app.services.retention_service import RetentionService


@pytest.mark.asyncio
async def test_retention_service_pruning():
    service = RetentionService()
    mock_supabase = MagicMock()

    mock_table = MagicMock()
    mock_update_chain = MagicMock()
    mock_update_chain.lt = MagicMock(return_value=mock_update_chain)
    mock_update_chain.neq = MagicMock(return_value=mock_update_chain)
    mock_update_chain.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "email-1"}, {"id": "email-2"}])
    )
    mock_table.update = MagicMock(return_value=mock_update_chain)

    mock_delete_chain = MagicMock()
    mock_delete_chain.lt = MagicMock(return_value=mock_delete_chain)
    mock_delete_chain.execute = AsyncMock(return_value=MagicMock(data=[{"id": "tok-1"}]))
    mock_table.delete = MagicMock(return_value=mock_delete_chain)

    mock_supabase.table = MagicMock(return_value=mock_table)
    service.client = mock_supabase

    pruned = await service.prune_old_email_bodies(retention_days=30)
    assert pruned == 2

    stats = await service.prune_expired_tokens_and_sessions()
    assert stats["sessions"] == 1
    assert stats["link_tokens"] == 1
    assert stats["callback_tokens"] == 1


def test_openapi_schema_generation():
    """Verify OpenAPI 3.1 specification generates valid JSON schema with all expected routes."""
    schema = app.openapi()
    assert schema is not None
    assert schema["info"]["title"] == "Email Agent API"
    assert schema["info"]["version"] == "1.0.0"

    paths = schema["paths"]
    # Check core endpoints exist
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/api/v1/auth/google/start" in paths
    assert "/api/v1/auth/google/callback" in paths
    assert "/api/v1/emails" in paths
    assert "/api/v1/drafts" in paths
    assert "/api/v1/telegram/webhook" in paths
    assert "/api/v1/features/rules" in paths
    assert "/api/v1/features/qa" in paths
