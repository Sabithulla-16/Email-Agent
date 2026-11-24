"""Feature-flagged extension endpoints (Rules, Smart Actions, Followups, Q&A, Extractors)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_user,
    get_email_repo,
    get_rule_repo,
    verify_csrf,
)
from app.core.errors import ForbiddenError, NotFoundError
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.models.db import (
    RuleCreate,
    RuleRow,
    UserRow,
)
from app.services.extra_extractors import ExtraExtractorsService
from app.services.followup_service import FollowupService
from app.services.inbox_qa import InboxQAService
from app.services.smart_actions import SmartActionsService

router = APIRouter(prefix="/features", tags=["Feature-Flagged Extensions"])


def check_feature_flag(user: UserRow, flag_name: str) -> None:
    """Enforce per-user feature flags from users.feature_flags jsonb column."""
    flags = user.feature_flags or {}
    # Default to True if flag not explicitly set to False, for smooth developer experience
    if flags.get(flag_name) is False:
        raise ForbiddenError(f"Feature '{flag_name}' is disabled for your account.")


# -----------------------------------------------------------------------------
# Rules Engine Endpoints (Flag: rules_engine)
# -----------------------------------------------------------------------------
class RuleCreatePayload(BaseModel):
    condition: dict[str, Any]
    action: dict[str, Any]
    trust_level: str = Field(default="suggest", description="suggest, act_notify, or auto")


@router.get("/rules", response_model=list[RuleRow])
async def list_rules(
    current_user: Annotated[UserRow, Depends(get_current_user)],
    rule_repo: Annotated[RuleRepo, Depends(get_rule_repo)],
) -> list[RuleRow]:
    """List automation rules for current user."""
    check_feature_flag(current_user, "rules_engine")
    return await rule_repo.list_by_user(current_user.id)


@router.post(
    "/rules",
    response_model=RuleRow,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
async def create_rule(
    payload: RuleCreatePayload,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    rule_repo: Annotated[RuleRepo, Depends(get_rule_repo)],
) -> RuleRow:
    """Create a new automation rule with trust ladder level."""
    check_feature_flag(current_user, "rules_engine")
    return await rule_repo.create_rule(
        RuleCreate(
            user_id=str(current_user.id),
            condition=payload.condition,
            action=payload.action,
            trust_level=payload.trust_level,
        )
    )


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_csrf)],
)
async def delete_rule(
    rule_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    rule_repo: Annotated[RuleRepo, Depends(get_rule_repo)],
) -> dict[str, Any]:
    """Delete an automation rule."""
    check_feature_flag(current_user, "rules_engine")
    deleted = await rule_repo.delete_rule(rule_id=rule_id, user_id=current_user.id)
    return {"deleted": deleted}


# -----------------------------------------------------------------------------
# Inbox Q&A Endpoint (Flag: inbox_qa)
# -----------------------------------------------------------------------------
@router.get("/qa")
async def inbox_qa(
    q: Annotated[str, Query(min_length=2, description="Natural language question")],
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
) -> dict[str, Any]:
    """Answer questions over user emails with grounded citations."""
    check_feature_flag(current_user, "inbox_qa")
    service = InboxQAService(email_repo=email_repo)
    return await service.answer_question(user_id=current_user.id, query=q)


# -----------------------------------------------------------------------------
# Smart Actions & Meeting Extraction (Flag: smart_actions)
# -----------------------------------------------------------------------------
@router.get("/smart-actions/{email_id}")
async def get_smart_actions(
    email_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
) -> dict[str, Any]:
    """Detect meetings and calendar links for an email."""
    check_feature_flag(current_user, "smart_actions")
    email = await email_repo.get_by_id(email_id)
    if not email or email.user_id != current_user.id:
        raise NotFoundError("Email not found")

    service = SmartActionsService()
    meeting = service.extract_meeting_details(email)
    return {"email_id": str(email_id), "meeting": meeting}


# -----------------------------------------------------------------------------
# Commitments & Followups (Flag: followups)
# -----------------------------------------------------------------------------
@router.get("/commitments")
async def list_commitments(
    current_user: Annotated[UserRow, Depends(get_current_user)],
) -> list[dict[str, Any]]:
    """List open commitments and promises detected in user outgoing messages."""
    check_feature_flag(current_user, "followups")
    service = FollowupService()
    return await service.list_open_commitments(current_user.id)


# -----------------------------------------------------------------------------
# Extra Extractors: Bills, Travel & Security (Flag: extra_extractors)
# -----------------------------------------------------------------------------
@router.get("/extractors/{email_id}")
async def run_extractors(
    email_id: UUID,
    current_user: Annotated[UserRow, Depends(get_current_user)],
    email_repo: Annotated[EmailRepo, Depends(get_email_repo)],
) -> dict[str, Any]:
    """Run bills, travel, and phishing warning extractors on an email."""
    check_feature_flag(current_user, "extra_extractors")
    email = await email_repo.get_by_id(email_id)
    if not email or email.user_id != current_user.id:
        raise NotFoundError("Email not found")

    service = ExtraExtractorsService()
    bill = service.extract_bill(email)
    travel = service.extract_travel_reservation(email)
    phishing = service.scan_phishing_indicators(email)

    return {
        "email_id": str(email_id),
        "bill": bill,
        "travel": travel,
        "phishing_warnings": phishing,
    }
