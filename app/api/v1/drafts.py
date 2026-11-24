"""Draft approval, revision, editing, and sending API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_auth_service,
    get_current_user,
    get_draft_repo,
    get_email_repo,
    get_user_repo,
    verify_csrf,
)
from app.core.errors import NotFoundError
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    DraftRow,
    DraftStatus,
    DraftVersionRow,
    UserRow,
)
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService

router = APIRouter(prefix="/drafts", tags=["Drafts & Approvals"])


def get_approval_service(
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApprovalService:
    return ApprovalService(
        draft_repo=draft_repo,
        email_repo=email_repo,
        user_repo=user_repo,
        auth_service=auth_service,
    )


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------
class DraftGenerateRequest(BaseModel):
    email_id: UUID


class DraftReviseRequest(BaseModel):
    instruction: str = Field(..., min_length=1, description="Revision instruction")
    tone: str | None = Field(
        None, description="Optional tone preset: shorter, formal, friendly, firm"
    )


class DraftEditRequest(BaseModel):
    body: str = Field(..., min_length=1, description="Updated draft body text")


class DraftDetailResponse(BaseModel):
    draft: DraftRow
    versions: list[DraftVersionRow]


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@router.get("", response_model=list[DraftRow])
async def list_drafts(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
    status_filter: Annotated[DraftStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DraftRow]:
    """List drafts for the current user, optionally filtered by status with pagination."""
    status_val = status_filter.value if status_filter else None
    return await draft_repo.list_by_user(
        user_id=current_user.id,
        status=status_val,
        limit=limit,
        offset=offset,
    )


@router.get("/{draft_id}", response_model=DraftDetailResponse)
async def get_draft(
    draft_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
) -> DraftDetailResponse:
    """Get draft details including all previous version revisions."""
    draft = await draft_repo.get_by_id(draft_id)
    if not draft or draft.user_id != current_user.id:
        raise NotFoundError(f"Draft {draft_id} not found")

    versions = await draft_repo.get_versions(draft_id)
    return DraftDetailResponse(draft=draft, versions=versions)


@router.post(
    "/generate",
    response_model=DraftRow,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
async def generate_draft(
    payload: DraftGenerateRequest,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> DraftRow:
    """Generate an AI reply draft for a specific email."""
    return await approval_service.generate_draft_for_email(
        email_id=payload.email_id,
        user_id=current_user.id,
    )


@router.post(
    "/{draft_id}/approve",
    response_model=DraftRow,
    dependencies=[Depends(verify_csrf)],
)
async def approve_draft(
    draft_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> DraftRow:
    """Explicitly approve a draft and send it via Gmail."""
    return await approval_service.approve_and_send(
        draft_id=draft_id,
        user_id=current_user.id,
    )


@router.post(
    "/{draft_id}/reject",
    response_model=DraftRow,
    dependencies=[Depends(verify_csrf)],
)
async def reject_draft(
    draft_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> DraftRow:
    """Reject a draft, closing it with REJECTED status."""
    return await approval_service.reject_draft(
        draft_id=draft_id,
        user_id=current_user.id,
    )


@router.post(
    "/{draft_id}/revise",
    response_model=DraftDetailResponse,
    dependencies=[Depends(verify_csrf)],
)
async def revise_draft(
    draft_id: UUID,
    payload: DraftReviseRequest,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
) -> DraftDetailResponse:
    """Revise a draft using Groq LLM with specific instructions or tone adjustments."""
    ready_draft, new_version = await approval_service.revise_draft(
        draft_id=draft_id,
        user_id=current_user.id,
        instruction=payload.instruction,
        tone=payload.tone,
    )
    all_versions = await draft_repo.get_versions(draft_id)
    return DraftDetailResponse(draft=ready_draft, versions=all_versions)


@router.post(
    "/{draft_id}/edit",
    response_model=DraftDetailResponse,
    dependencies=[Depends(verify_csrf)],
)
async def edit_draft(
    draft_id: UUID,
    payload: DraftEditRequest,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    draft_repo: Annotated[DraftRepo, Depends(get_draft_repo)],
) -> DraftDetailResponse:
    """Manually edit the draft body text."""
    draft, new_version = await approval_service.edit_draft_manually(
        draft_id=draft_id,
        user_id=current_user.id,
        new_body=payload.body,
    )
    all_versions = await draft_repo.get_versions(draft_id)
    return DraftDetailResponse(draft=draft, versions=all_versions)
