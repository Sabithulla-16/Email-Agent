"""Email management, manual sync, and feedback API endpoints."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    get_auth_service,
    get_current_user,
    get_email_repo,
    get_rule_repo,
    get_user_repo,
    verify_csrf,
)
from app.core.errors import NotFoundError
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    EmailListItem,
    FeedbackCreate,
    FeedbackRow,
    UserRow,
)
from app.services.auth_service import AuthService
from app.services.sync_service import SyncService

router = APIRouter(tags=["Emails & Sync"])


def get_sync_service(
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> SyncService:
    return SyncService(user_repo=user_repo, email_repo=email_repo, auth_service=auth_service)


@router.post("/sync", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_csrf)])
async def trigger_email_sync(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    sync_service: Annotated[SyncService, Depends(get_sync_service)],
) -> dict[str, Any]:
    """Manually trigger email synchronization from Gmail for the current user."""
    result = await sync_service.sync_user_emails(current_user.id)
    return {
        "status": "completed",
        "result": result,
    }


@router.get("/emails", response_model=list[EmailListItem])
async def list_emails(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EmailListItem]:
    """List emails with pagination.

    Excludes body_clean from list projections for maximum throughput.
    """
    return await email_repo.list_by_user(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )


@router.get("/emails/{email_id}")
async def get_email_detail(
    email_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
) -> dict[str, Any]:
    """Get full email message including sanitized body and analysis."""
    email = await email_repo.get_by_id(email_id)
    if not email or email.user_id != current_user.id:
        raise NotFoundError(f"Email {email_id} not found")

    analysis = await email_repo.get_analysis(email_id)

    return {
        "email": email.model_dump(),
        "analysis": analysis.model_dump() if analysis else None,
    }


@router.post("/emails/{email_id}/feedback", dependencies=[Depends(verify_csrf)])
async def submit_email_feedback(
    email_id: UUID,
    rating: Annotated[int, Query(ge=1, le=5)],
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
    rule_repo: Annotated[RuleRepo, Depends(get_rule_repo)],
    note: str | None = None,
) -> FeedbackRow:
    """Submit user feedback for email analysis / draft quality."""
    email = await email_repo.get_by_id(email_id)
    if not email or email.user_id != current_user.id:
        raise NotFoundError(f"Email {email_id} not found")

    return await rule_repo.create_feedback(
        FeedbackCreate(
            user_id=str(current_user.id),
            entity_type="email",
            entity_id=str(email_id),
            rating=rating,
            note=note,
        )
    )
