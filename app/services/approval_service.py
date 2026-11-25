"""Draft Generation, Revision Loop, and Approval State Machine."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import (
    DraftCreate,
    DraftRow,
    DraftStatus,
    DraftVersionRow,
    EmailCreate,
    EmailRow,
)
from app.services.auth_service import AuthService
from app.services.gmail_client import GmailClient
from app.services.groq_service import GroqService
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
        groq_service: GroqService | None = None,
    ) -> None:
        self.draft_repo = draft_repo
        self.email_repo = email_repo
        self.user_repo = user_repo
        self.auth_service = auth_service
        self.settings = settings or get_settings()
        self.groq_service = groq_service or GroqService(settings=self.settings)
        self.groq_client = self.groq_service.client

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
    async def generate_draft_for_email(
        self,
        email_id: UUID | str,
        user_id: UUID | str,
        api_key: str | None = None,
    ) -> DraftRow:
        """Generate AI reply draft for an email with need_reply=True and no high risk flags."""
        email = await self.email_repo.get_by_id(email_id)
        if not email or email.user_id != UUID(str(user_id)):
            raise NotFoundError(f"Email {email_id} not found")

        # Check if draft already exists for this email
        try:
            if hasattr(self.draft_repo, "get_by_email_id"):
                res = self.draft_repo.get_by_email_id(email_id)
                if hasattr(res, "__await__"):
                    existing_draft = await res
                    if existing_draft:
                        logger.info(
                            "draft_already_exists_for_email",
                            email_id=str(email_id),
                            draft_id=str(existing_draft.id),
                            status=existing_draft.status.value,
                        )
                        return existing_draft
        except Exception as exc:
            logger.warning("draft_check_existing_failed", email_id=str(email_id), error=str(exc))

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

        # Resolve user-specific Groq API key (BYOK) if configured
        user_key = api_key
        if not user_key and hasattr(self.auth_service, "get_user_groq_api_key"):
            try:
                res_key: Any = self.auth_service.get_user_groq_api_key(user_id)
                if hasattr(res_key, "__await__"):
                    user_key = await res_key
                elif isinstance(res_key, str):
                    user_key = res_key
            except Exception:
                user_key = None

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

        draft_text = ""
        model_used = self.settings.GROQ_DRAFT_MODEL
        try:
            draft_text, model_used = await self.groq_service.chat_completion_with_fallback(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                primary_model=self.settings.GROQ_DRAFT_MODEL,
                temperature=0.2,
                api_key=user_key,
                client_override=(None if (user_key and user_key.strip()) else self.groq_client),
            )
        except Exception as exc:
            logger.warning("draft_all_models_failed_using_fallback", error=str(exc))

        if not draft_text:
            draft_text = (
                f"Hi {email.sender},\n\n"
                f"Thank you for your email. I will follow up with you shortly.\n\n"
                f"{sig}"
            )

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
            model=model_used,
        )

        # Transition DRAFT_GENERATED -> PENDING_APPROVAL
        return await self.transition(draft, DraftStatus.PENDING_APPROVAL)

    async def create_custom_draft(
        self,
        user_id: UUID | str,
        recipient: str,
        user_instruction: str,
        subject: str | None = None,
        api_key: str | None = None,
    ) -> tuple[DraftRow, EmailRow]:
        """Compose a new outbound email draft from user instructions using Groq LLM.

        Rewrites rough notes into a polished email, connects it with the standard
        versioning and approval pipeline, and returns (draft, email).
        """
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        # Resolve user Groq API key
        user_key = api_key
        if not user_key and hasattr(self.auth_service, "get_user_groq_api_key"):
            try:
                res_key: Any = self.auth_service.get_user_groq_api_key(user_id)
                if hasattr(res_key, "__await__"):
                    user_key = await res_key
                elif isinstance(res_key, str):
                    user_key = res_key
            except Exception:
                user_key = None

        tone = user.prefs.get("tone", "friendly")
        user_name = user.name or "User"
        sig = user.prefs.get("signature") or f"Best regards,\n{user_name}"

        sys_prompt = (
            "You are an executive email writing assistant. "
            "The user wants to compose an email to the specified recipient based on their notes.\n"
            f"Tone: {tone}\n"
            f"Sign-off Signature:\n{sig}\n\n"
            "INSTRUCTIONS:\n"
            "1. Write a clear, professional email matching the requested tone.\n"
            "2. If a subject is given, refine it; otherwise create a concise subject line.\n"
            "3. Output strictly valid JSON matching this schema:\n"
            '{"subject": "Concise subject", "body": "Full body with greeting and signature"}'
        )

        user_content = (
            f"Recipient: {recipient}\n"
            f"Provided Subject: {subject or 'None'}\n"
            f"User Instructions / Notes:\n{user_instruction}"
        )

        parsed_subject = subject or "Quick update"
        parsed_body = ""
        model_used = self.settings.GROQ_DRAFT_MODEL

        try:
            raw, model_used = await self.groq_service.chat_completion_with_fallback(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                primary_model=self.settings.GROQ_DRAFT_MODEL,
                temperature=0.2,
                api_key=user_key,
                response_format={"type": "json_object"},
                client_override=(None if (user_key and user_key.strip()) else self.groq_client),
            )
            data = json.loads(raw)
            parsed_subject = str(data.get("subject", subject or "Quick update")).strip()
            parsed_body = str(data.get("body", "")).strip()
        except Exception as exc:
            logger.warning("compose_draft_all_models_failed", error=str(exc))

        if not parsed_body:
            parsed_body = f"Hi,\n\n{user_instruction}\n\n{sig}"

        # 1. Create an outbound synthetic email record in DB
        now_dt = datetime.now(UTC)
        outbound_msg_id = f"outbound_{uuid.uuid4().hex[:12]}"
        created_email = await self.email_repo.upsert_email(
            EmailCreate(
                user_id=str(user.id),
                gmail_msg_id=outbound_msg_id,
                thread_id=f"th_{outbound_msg_id}",
                rfc_message_id=None,
                sender=f"{user_name} <{user.email}>",
                sender_email=recipient,
                reply_to=recipient,
                subject=parsed_subject,
                received_at=now_dt.isoformat(),
                snippet=parsed_body[:200],
                body_clean=user_instruction,
                has_attachments=False,
                labels=["COMPOSED", "OUTBOUND"],
            )
        )

        # 2. Create draft record in drafts table
        idempotency_key = f"draft_{uuid.uuid4().hex[:16]}"
        expires_at = now_dt + timedelta(hours=self.settings.DRAFT_EXPIRY_HOURS)

        draft = await self.draft_repo.create_draft(
            DraftCreate(
                user_id=str(user.id),
                email_id=str(created_email.id),
                status=DraftStatus.DRAFT_GENERATED.value,
                idempotency_key=idempotency_key,
                expires_at=expires_at.isoformat(),
            )
        )

        # 3. Create version 1 record
        await self.draft_repo.create_version(
            draft_id=draft.id,
            body=parsed_body,
            user_instruction=user_instruction,
            source="llm",
            model=model_used,
        )

        # 4. Transition to PENDING_APPROVAL
        ready_draft = await self.transition(draft, DraftStatus.PENDING_APPROVAL)
        return ready_draft, created_email

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

        # 3. Build RFC 2822 MIME reply/message
        reply_to_target = email.reply_to or email.sender_email
        is_composed = "COMPOSED" in (email.labels or [])
        is_reply = not is_composed
        thread_id = None if is_composed else email.thread_id

        raw_b64 = build_mime_reply(
            from_addr=f"{user.name or ''} <{user.email}>".strip(),
            to_addr=reply_to_target,
            subject=email.subject,
            body=latest_version.body,
            in_reply_to=None if is_composed else email.rfc_message_id,
            references=None if is_composed else email.rfc_message_id,
            idempotency_key=draft.idempotency_key,
            is_reply=is_reply,
        )

        # 4. Send via Gmail API
        try:
            token = await self.auth_service.get_valid_access_token(user_id)
            client = GmailClient(access_token=token)
            send_resp = await client.send_raw_message(raw_b64=raw_b64, thread_id=thread_id)
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
        api_key: str | None = None,
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

        # Resolve user-specific Groq API key (BYOK) if configured
        user_key = api_key
        if not user_key and hasattr(self.auth_service, "get_user_groq_api_key"):
            try:
                res_key: Any = self.auth_service.get_user_groq_api_key(user_id)
                if hasattr(res_key, "__await__"):
                    user_key = await res_key
                elif isinstance(res_key, str):
                    user_key = res_key
            except Exception:
                user_key = None

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

        try:
            new_body, model_name = await self.groq_service.chat_completion_with_fallback(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                primary_model=self.settings.GROQ_DRAFT_MODEL,
                temperature=0.2,
                api_key=user_key,
                client_override=(None if (user_key and user_key.strip()) else self.groq_client),
            )
        except Exception as exc:
            logger.error("revise_draft_all_models_failed", draft_id=str(draft.id), error=str(exc))
            await self.transition(
                revising_draft,
                DraftStatus.PENDING_APPROVAL,
                failure_reason="LLM revision temporarily unavailable",
            )
            raise ExternalServiceError(
                "AI revision is temporarily unavailable. Please try again in a moment."
            ) from exc

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
