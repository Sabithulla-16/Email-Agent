"""Draft Generation, Revision Loop, and Approval State Machine."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import structlog
from groq import AsyncGroq

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    DraftCreate,
    DraftRow,
    DraftStatus,
    DraftVersionRow,
)
from app.services.auth_service import AuthService
from app.services.gmail_client import GmailClient
from app.services.mime_builder import build_mime_reply

logger = structlog.get_logger("app.services.approval")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

VALID_TRANSITIONS: dict[DraftStatus, set[DraftStatus]] = {
    DraftStatus.DRAFT_GENERATED: {DraftStatus.PENDING_APPROVAL},
    DraftStatus.PENDING_APPROVAL: {
        DraftStatus.APPROVED,
        DraftStatus.REJECTED,
        DraftStatus.REVISING,
        DraftStatus.EXPIRED,
    },
    DraftStatus.APPROVED: {DraftStatus.SENDING, DraftStatus.FAILED},
    DraftStatus.SENDING: {DraftStatus.SENT, DraftStatus.FAILED},
    DraftStatus.REVISING: {DraftStatus.PENDING_APPROVAL, DraftStatus.FAILED},
    DraftStatus.FAILED: {DraftStatus.SENDING, DraftStatus.REJECTED},
    DraftStatus.SENT: set(),
    DraftStatus.REJECTED: set(),
    DraftStatus.EXPIRED: set(),
}

TONE_INSTRUCTIONS: dict[str, str] = {
    "shorter": "Make the reply significantly shorter, concise, and straight to the point.",
    "formal": "Make the tone formal, highly professional, and respectful.",
    "friendly": "Make the tone warm, welcoming, friendly, and enthusiastic.",
    "firm": "Make the tone firm, direct, assertive, and unambiguous.",
}


class ApprovalService:
    """Owns every draft state transition and sending execution."""

    def __init__(
        self,
        draft_repo: DraftRepo,
        email_repo: EmailRepo,
        user_repo: UserRepo,
        auth_service: AuthService,
        settings: Settings | None = None,
    ) -> None:
        self.draft_repo = draft_repo
        self.email_repo = email_repo
        self.user_repo = user_repo
        self.auth_service = auth_service
        self.settings = settings or get_settings()
        self.groq_client = AsyncGroq(api_key=self.settings.GROQ_API_KEY.get_secret_value())

    # -------------------------------------------------------------------------
    # State Transition Enforcer
    # -------------------------------------------------------------------------
    async def transition(
        self,
        draft: DraftRow,
        to_status: DraftStatus,
        failure_reason: str | None = None,
        sent_gmail_msg_id: str | None = None,
    ) -> DraftRow:
        """Atomic Compare-and-Set transition backed by SQL function."""
        current_status = draft.status
        allowed = VALID_TRANSITIONS.get(current_status, set())
        if to_status not in allowed:
            raise ConflictError(
                f"Invalid draft transition from '{current_status.value}' to '{to_status.value}'. "
                f"Allowed target states: {[s.value for s in allowed]}"
            )

        updated_draft = await self.draft_repo.transition_draft(
            draft_id=draft.id,
            from_status=current_status.value,
            to_status=to_status.value,
            failure_reason=failure_reason,
            sent_gmail_msg_id=sent_gmail_msg_id,
        )
        logger.info(
            "draft_status_transitioned",
            draft_id=str(draft.id),
            from_status=current_status.value,
            to_status=to_status.value,
        )
        return updated_draft

    # -------------------------------------------------------------------------
    # Draft Generation
    # -------------------------------------------------------------------------
    async def generate_draft_for_email(self, email_id: UUID | str, user_id: UUID | str) -> DraftRow:
        """Generate AI reply draft for an email with need_reply=True and no high risk flags."""
        email = await self.email_repo.get_by_id(email_id)
        if not email or email.user_id != UUID(str(user_id)):
            raise NotFoundError(f"Email {email_id} not found")

        # Check analysis safety & necessity
        analysis = await self.email_repo.get_analysis(email_id)
        if analysis:
            if not analysis.needs_reply:
                raise ValidationError("Email does not require a reply based on AI analysis.")
            if any("phishing" in f or "malware" in f for f in analysis.risk_flags):
                raise ValidationError("Draft generation blocked due to security risk flags.")

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        # Load draft generation prompt
        prompt_template = (PROMPTS_DIR / "draft_v1.txt").read_text(encoding="utf-8")
        tone = user.prefs.get("tone", "friendly")
        user_name = user.name or "User"
        sig = user.prefs.get("signature") or f"Best regards,\n{user_name}"
        system_prompt = prompt_template.format(tone=tone, signature=sig)

        user_content = (
            f"Sender: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"<untrusted_email_content>\n"
            f"{email.body_clean}\n"
            f"</untrusted_email_content>"
        )

        model_name = self.settings.GROQ_DRAFT_MODEL
        resp = await self.groq_client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        draft_text = (resp.choices[0].message.content or "").strip()

        # Create draft record (DRAFT_GENERATED)
        idempotency_key = f"draft_{uuid.uuid4().hex[:16]}"
        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.DRAFT_EXPIRY_HOURS)

        draft = await self.draft_repo.create_draft(
            DraftCreate(
                user_id=str(user_id),
                email_id=str(email_id),
                status=DraftStatus.DRAFT_GENERATED.value,
                idempotency_key=idempotency_key,
                expires_at=expires_at.isoformat(),
            )
        )

        # Create version 1 record
        await self.draft_repo.create_version(
            draft_id=draft.id,
            body=draft_text,
            user_instruction="Initial automated draft",
            source="llm",
            model=model_name,
        )

        # Transition DRAFT_GENERATED -> PENDING_APPROVAL
        return await self.transition(draft, DraftStatus.PENDING_APPROVAL)

    # -------------------------------------------------------------------------
    # Approval & Sending Execution
    # -------------------------------------------------------------------------
    async def approve_and_send(self, draft_id: UUID | str, user_id: UUID | str) -> DraftRow:
        """Approve draft and execute sending via Gmail with strict idempotency."""
        draft = await self._get_user_draft(draft_id, user_id)

        # 1. Transition PENDING_APPROVAL -> APPROVED
        approved_draft = await self.transition(draft, DraftStatus.APPROVED)

        # 2. Transition APPROVED -> SENDING
        sending_draft = await self.transition(approved_draft, DraftStatus.SENDING)

        email = await self.email_repo.get_by_id(draft.email_id)
        if not email:
            await self.transition(
                sending_draft,
                DraftStatus.FAILED,
                failure_reason="Original email missing",
            )
            raise NotFoundError("Original email not found")

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            await self.transition(
                sending_draft,
                DraftStatus.FAILED,
                failure_reason="User missing",
            )
            raise NotFoundError("User not found")

        # Fetch latest draft version body
        versions = await self.draft_repo.get_versions(draft.id)
        if not versions:
            await self.transition(
                sending_draft,
                DraftStatus.FAILED,
                failure_reason="Draft versions missing",
            )
            raise NotFoundError("Draft version body not found")
        latest_version = versions[-1]

        # 3. Build RFC 2822 MIME reply message
        reply_to_target = email.reply_to or email.sender_email
        raw_b64 = build_mime_reply(
            from_addr=f"{user.name or ''} <{user.email}>".strip(),
            to_addr=reply_to_target,
            subject=email.subject,
            body=latest_version.body,
            in_reply_to=email.rfc_message_id,
            references=email.rfc_message_id,
            idempotency_key=draft.idempotency_key,
        )

        # 4. Send via Gmail API
        try:
            token = await self.auth_service.get_valid_access_token(user_id)
            client = GmailClient(access_token=token)
            send_resp = await client.send_raw_message(raw_b64=raw_b64, thread_id=email.thread_id)
            sent_msg_id = send_resp.get("id")

            # 5. Transition SENDING -> SENT
            return await self.transition(
                sending_draft,
                DraftStatus.SENT,
                sent_gmail_msg_id=sent_msg_id,
            )
        except Exception as exc:
            logger.error("draft_send_execution_failed", draft_id=str(draft.id), error=str(exc))
            # Transition SENDING -> FAILED
            await self.transition(
                sending_draft,
                DraftStatus.FAILED,
                failure_reason=str(exc),
            )
            raise

    # -------------------------------------------------------------------------
    # Rejection
    # -------------------------------------------------------------------------
    async def reject_draft(self, draft_id: UUID | str, user_id: UUID | str) -> DraftRow:
        """Reject draft and transition PENDING_APPROVAL -> REJECTED."""
        draft = await self._get_user_draft(draft_id, user_id)
        return await self.transition(draft, DraftStatus.REJECTED)

    # -------------------------------------------------------------------------
    # Revision Loop
    # -------------------------------------------------------------------------
    async def revise_draft(
        self,
        draft_id: UUID | str,
        user_id: UUID | str,
        instruction: str,
        tone: str | None = None,
    ) -> tuple[DraftRow, DraftVersionRow]:
        """Revise draft with Groq LLM using feedback instructions or tone presets."""
        draft = await self._get_user_draft(draft_id, user_id)

        # Transition PENDING_APPROVAL -> REVISING
        revising_draft = await self.transition(draft, DraftStatus.REVISING)

        email = await self.email_repo.get_by_id(draft.email_id)
        if not email:
            raise NotFoundError("Original email context not found")

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Fetch previous version body
        versions = await self.draft_repo.get_versions(draft.id)
        previous_draft_body = versions[-1].body if versions else ""

        # Map tone button if provided
        tone_instruction = TONE_INSTRUCTIONS.get(tone.lower(), "") if tone else ""
        combined_instruction = f"{instruction} {tone_instruction}".strip()

        # Load revision prompt
        prompt_template = (PROMPTS_DIR / "revision_v1.txt").read_text(encoding="utf-8")
        user_name = user.name or "User"
        sig = user.prefs.get("signature") or f"Best regards,\n{user_name}"
        sys_prompt = prompt_template.format(
            tone=tone or user.prefs.get("tone", "friendly"),
            signature=sig,
        )

        user_content = (
            f"<original_thread_context>\n"
            f"Sender: {email.sender}\n"
            f"Subject: {email.subject}\n"
            f"{email.body_clean}\n"
            f"</original_thread_context>\n\n"
            f"<previous_draft>\n"
            f"{previous_draft_body}\n"
            f"</previous_draft>\n\n"
            f"<user_revision_instruction>\n"
            f"{combined_instruction}\n"
            f"</user_revision_instruction>"
        )

        model_name = self.settings.GROQ_DRAFT_MODEL
        resp = await self.groq_client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        new_body = (resp.choices[0].message.content or "").strip()

        # Increment version in database
        new_version = await self.draft_repo.create_version(
            draft_id=draft.id,
            body=new_body,
            user_instruction=combined_instruction,
            source="revision",
            model=model_name,
        )

        # Transition REVISING -> PENDING_APPROVAL
        ready_draft = await self.transition(revising_draft, DraftStatus.PENDING_APPROVAL)
        return ready_draft, new_version

    async def edit_draft_manually(
        self, draft_id: UUID | str, user_id: UUID | str, new_body: str
    ) -> tuple[DraftRow, DraftVersionRow]:
        """Manually edit draft body directly from the web interface."""
        draft = await self._get_user_draft(draft_id, user_id)
        if draft.status not in {DraftStatus.PENDING_APPROVAL, DraftStatus.DRAFT_GENERATED}:
            raise ConflictError(f"Cannot edit draft in status {draft.status.value}")

        new_version = await self.draft_repo.create_version(
            draft_id=draft.id,
            body=new_body,
            user_instruction="Manual user edit",
            source="user_edit",
            model="human",
        )
        return draft, new_version

    async def _get_user_draft(self, draft_id: UUID | str, user_id: UUID | str) -> DraftRow:
        draft = await self.draft_repo.get_by_id(draft_id)
        if not draft or draft.user_id != UUID(str(user_id)):
            raise NotFoundError(f"Draft {draft_id} not found")
        return draft
