"""Scheduled outbound email service: natural time parsing, encryption, and auto-dispatch."""

import json
import re
import zoneinfo
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.vault import TokenVault, get_token_vault
from app.db.client import get_supabase
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.services.auth_service import AuthService
from app.services.gmail_client import GmailClient
from app.services.groq_service import GroqService
from app.services.mime_builder import build_mime_reply
from app.services.telegram_client import TelegramClient, escape_html

logger = structlog.get_logger("app.services.scheduled_email")

_REL_RE = re.compile(
    r"^\s*(?:in\s+)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_schedule_time(
    text: str,
    user_timezone: str = "Asia/Kolkata",
    now: datetime | None = None,
) -> datetime | None:
    """Parse natural/explicit time expressions in user_timezone into a future UTC datetime."""
    raw = text.strip()
    if not raw:
        return None

    try:
        tz: Any = zoneinfo.ZoneInfo(user_timezone)
    except Exception:
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")

    now_utc = (
        now.astimezone(UTC)
        if now and now.tzinfo
        else (now.replace(tzinfo=UTC) if now else datetime.now(UTC))
    )
    local_now = now_utc.astimezone(tz)

    # 1. Relative offset: "in 30m", "2h", "in 1 hour"
    rel_m = _REL_RE.match(raw)
    if rel_m:
        qty = int(rel_m.group(1))
        unit = rel_m.group(2).lower()
        if qty <= 0:
            return None
        delta = timedelta(minutes=qty) if unit.startswith("m") else timedelta(hours=qty)
        return (now_utc + delta).replace(second=0, microsecond=0)

    # 2. Explicit date (YYYY-MM-DD) or today/tomorrow + time
    lower = raw.lower()
    target_date = local_now.date()
    explicit_date = False

    iso_m = _ISO_DATE_RE.search(raw)
    if iso_m:
        try:
            target_date = datetime.strptime(iso_m.group(1), "%Y-%m-%d").date()
            explicit_date = True
            rest = raw.replace(iso_m.group(1), " ").strip()
        except ValueError:
            return None
    elif "tomorrow" in lower:
        target_date = local_now.date() + timedelta(days=1)
        explicit_date = True
        rest = re.sub(r"(?i)\btomorrow\b", " ", raw).strip()
    else:
        rest = re.sub(r"(?i)\btoday\b", " ", raw).strip()

    time_m = _TIME_RE.search(rest)
    if not time_m:
        return None

    hour = int(time_m.group(1))
    minute = int(time_m.group(2) or 0)
    ampm = (time_m.group(3) or "").lower()

    if minute < 0 or minute > 59:
        return None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    elif hour > 23:
        return None

    candidate_local = datetime.combine(target_date, time(hour, minute), tzinfo=tz)
    if not explicit_date and candidate_local <= local_now:
        candidate_local = datetime.combine(
            target_date + timedelta(days=1), time(hour, minute), tzinfo=tz
        )

    if candidate_local <= local_now:
        return None
    return candidate_local.astimezone(UTC)


class ScheduledEmailService:
    """Creates, lists, cancels, and auto-dispatches encrypted scheduled emails."""

    def __init__(
        self,
        client: Any = None,
        settings: Settings | None = None,
        vault: TokenVault | None = None,
        groq_service: GroqService | None = None,
    ) -> None:
        self._client = client
        self.settings = settings or get_settings()
        self._vault = vault
        self.groq_service = groq_service or GroqService(settings=self.settings)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_supabase()
        return self._client

    @property
    def vault(self) -> TokenVault:
        if self._vault is None:
            self._vault = get_token_vault()
        return self._vault

    async def compose_subject_and_body(
        self,
        recipient: str,
        notes: str,
        user_name: str | None = None,
        api_key: str | None = None,
    ) -> tuple[str, str]:
        """Polish user notes into (subject, body) using Groq LLM with safe fallback."""
        prompt = (
            "Write a clear, professional email based on the user's notes.\n"
            f"Sender name: {user_name or 'User'}\n"
            f"Recipient: {recipient}\n"
            f"Notes: {notes}\n\n"
            'Return strictly valid JSON: {"subject": "...", "body": "..."}'
        )
        try:
            raw_json, _ = await self.groq_service.chat_completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
                api_key=api_key,
            )
            cleaned = raw_json.strip()
            if "```" in cleaned:
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL)
            parsed = json.loads(cleaned)
            subj = str(parsed.get("subject") or "").strip()
            body = str(parsed.get("body") or "").strip()
            if subj and body:
                return subj, body
        except Exception as exc:
            logger.debug("schedule_compose_llm_fallback", error=str(exc))

        first_line = notes.strip().splitlines()[0][:60]
        return f"Message: {first_line}", notes.strip()

    async def create_scheduled_email(
        self,
        user_id: UUID | str,
        recipient: str,
        subject: str,
        body: str,
        scheduled_at: datetime,
    ) -> dict[str, Any]:
        """Encrypt and persist a scheduled email in Supabase."""
        sched_utc = (
            scheduled_at.astimezone(UTC)
            if scheduled_at.tzinfo
            else scheduled_at.replace(tzinfo=UTC)
        )
        payload = {
            "user_id": str(user_id),
            "recipient_enc": self.vault.encrypt_field(recipient),
            "subject_enc": self.vault.encrypt_field(subject),
            "body_enc": self.vault.encrypt_field(body),
            "scheduled_at": sched_utc.isoformat(),
            "status": "pending",
        }
        res = await self.client.table("scheduled_emails").insert(payload).execute()
        row = dict(cast(dict[str, Any], res.data[0]))
        return self._decrypt_row(row)

    async def list_pending_for_user(
        self, user_id: UUID | str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """List pending scheduled emails for a single user."""
        res = await (
            self.client.table("scheduled_emails")
            .select("*")
            .eq("user_id", str(user_id))
            .eq("status", "pending")
            .order("scheduled_at", desc=False)
            .limit(limit)
            .execute()
        )
        return [self._decrypt_row(dict(cast(dict[str, Any], r))) for r in (res.data or [])]

    async def cancel_scheduled_email(
        self, user_id: UUID | str, scheduled_id: UUID | str
    ) -> bool:
        """Cancel a pending scheduled email owned by user_id."""
        res = await (
            self.client.table("scheduled_emails")
            .update({"status": "cancelled"})
            .eq("id", str(scheduled_id))
            .eq("user_id", str(user_id))
            .eq("status", "pending")
            .execute()
        )
        return bool(res.data)

    async def dispatch_due_scheduled_emails(self, limit: int = 20) -> dict[str, int]:
        """Find all due pending scheduled emails, send via user's Gmail, and notify Telegram."""
        now_iso = datetime.now(UTC).isoformat()
        res = await (
            self.client.table("scheduled_emails")
            .select("*")
            .eq("status", "pending")
            .lte("scheduled_at", now_iso)
            .order("scheduled_at", desc=False)
            .limit(limit)
            .execute()
        )
        due_rows = list(res.data or [])
        if not due_rows:
            return {"processed": 0, "sent": 0, "failed": 0}

        user_repo = UserRepo(self.client, vault=self.vault)
        auth_svc = AuthService(user_repo=user_repo, settings=self.settings, vault=self.vault)
        tg_repo = TelegramRepo(self.client)
        tg_client = TelegramClient(telegram_repo=tg_repo, settings=self.settings)

        sent_count = 0
        failed_count = 0

        for raw_row in due_rows:
            row = self._decrypt_row(dict(cast(dict[str, Any], raw_row)))
            sched_id = str(row["id"])
            user_id = str(row["user_id"])

            # Atomically claim pending -> sending
            claim = await (
                self.client.table("scheduled_emails")
                .update({"status": "sending"})
                .eq("id", sched_id)
                .eq("status", "pending")
                .execute()
            )
            if not claim.data:
                continue

            try:
                user = await user_repo.get_by_id(user_id)
                if not user:
                    raise RuntimeError("User not found")
                token = await auth_svc.get_valid_access_token(user_id)
                gmail = GmailClient(access_token=token)
                raw_b64 = build_mime_reply(
                    from_addr=user.email,
                    to_addr=row["recipient"],
                    subject=row["subject"],
                    body=row["body"],
                    is_reply=False,
                )
                send_res = await gmail.send_raw_message(raw_b64)
                gmail_msg_id = str(send_res.get("id") or "")

                await (
                    self.client.table("scheduled_emails")
                    .update({"status": "sent", "sent_gmail_msg_id": gmail_msg_id})
                    .eq("id", sched_id)
                    .execute()
                )
                sent_count += 1

                tg_link = await tg_repo.get_link_by_user_id(user_id)
                if tg_link and tg_link.is_active:
                    await tg_client.send_message(
                        chat_id=tg_link.chat_id,
                        text=(
                            "✓ <b>Scheduled Email Sent</b>\n\n"
                            f"• <b>To:</b> <code>{escape_html(row['recipient'])}</code>\n"
                            f"• <b>Subject:</b> {escape_html(row['subject'])}"
                        ),
                    )
            except Exception as exc:
                failed_count += 1
                logger.error("scheduled_email_dispatch_failed", id=sched_id, error=str(exc))
                await (
                    self.client.table("scheduled_emails")
                    .update({"status": "failed", "failure_reason": str(exc)[:500]})
                    .eq("id", sched_id)
                    .execute()
                )
                tg_link = await tg_repo.get_link_by_user_id(user_id)
                if tg_link and tg_link.is_active:
                    await tg_client.send_message(
                        chat_id=tg_link.chat_id,
                        text=(
                            "✕ <b>Scheduled Email Failed</b>\n\n"
                            f"• <b>To:</b> <code>{escape_html(row['recipient'])}</code>\n"
                            f"• <b>Reason:</b> {escape_html(str(exc))}"
                        ),
                    )

        return {"processed": len(due_rows), "sent": sent_count, "failed": failed_count}

    def _decrypt_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "recipient": self.vault.decrypt_field(row.get("recipient_enc")) or "",
            "subject": self.vault.decrypt_field(row.get("subject_enc")) or "",
            "body": self.vault.decrypt_field(row.get("body_enc")) or "",
        }
