"""Follow-up and commitment tracking service."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import structlog

from app.core.vault import get_token_vault
from app.db.client import get_supabase

logger = structlog.get_logger("app.services.followup")

COMMITMENT_PATTERNS = [
    r"\bi will\s+([a-zA-Z\s]{5,50}?)(?:\s+by|\s+tomorrow|\s+next|\.|\,)",
    r"\bi'll\s+([a-zA-Z\s]{5,50}?)(?:\s+by|\s+tomorrow|\s+next|\.|\,)",
    r"\bi promise to\s+([a-zA-Z\s]{5,50}?)(?:\s+by|\s+tomorrow|\.|\,)",
]


class FollowupService:
    """Detects and tracks pending follow-ups and outgoing user commitments."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_supabase()
        return self._client

    def detect_commitments(self, text: str) -> list[str]:
        """Extract explicit user promises and commitments from sent messages."""
        commitments: list[str] = []
        for pat in COMMITMENT_PATTERNS:
            matches = re.finditer(pat, text, re.IGNORECASE)
            for m in matches:
                clause = m.group(1).strip()
                if clause and len(clause) > 5:
                    commitments.append(f"Promise: {clause}")
        return commitments

    async def register_commitment(
        self,
        user_id: UUID | str,
        email_id: UUID | str | None,
        promise_text: str,
        days_due: int = 3,
    ) -> dict[str, Any] | None:
        """Store tracked commitment in database."""
        now = datetime.now(UTC)
        deadline = (now + timedelta(days=days_due)).date()

        vault = get_token_vault()
        payload = {
            "user_id": str(user_id),
            "email_id": str(email_id) if email_id else None,
            "promise": vault.encrypt_field(promise_text),
            "promised_to": vault.encrypt_field("Recipient"),
            "due_date": deadline.isoformat(),
            "status": "open",
            "created_at": now.isoformat(),
        }
        try:
            res = await self.client.table("commitments").insert(payload).execute()
            if res.data:
                row = dict(cast(dict[str, Any], res.data[0]))
                row["promise"] = vault.decrypt_field(row.get("promise"))
                row["promised_to"] = vault.decrypt_field(row.get("promised_to"))
                return row
            return None
        except Exception as exc:
            logger.debug("register_commitment_failed", error=str(exc))
            return None

    async def list_open_commitments(self, user_id: UUID | str) -> list[dict[str, Any]]:
        """List open commitments for a user."""
        vault = get_token_vault()
        try:
            res = await (
                self.client.table("commitments")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("status", "open")
                .order("created_at", desc=True)
                .execute()
            )
            rows = []
            for item in res.data or []:
                row = dict(cast(dict[str, Any], item))
                row["promise"] = vault.decrypt_field(row.get("promise"))
                row["promised_to"] = vault.decrypt_field(row.get("promised_to"))
                rows.append(row)
            return rows
        except Exception as exc:
            logger.debug("list_commitments_failed", error=str(exc))
            return []

    async def get_radar_summary(
        self,
        user_id: UUID | str,
        email_repo: Any | None = None,
        limit: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        """Compile 'People waiting on you' and 'People you are waiting on' for user_id."""
        waiting_on_you: list[dict[str, Any]] = []
        waiting_on_others: list[dict[str, Any]] = []

        if email_repo is not None:
            try:
                recent_emails = await email_repo.list_by_user(user_id=user_id, limit=25)
                for em in recent_emails:
                    analysis = await email_repo.get_analysis(em.id)
                    if analysis and analysis.needs_reply:
                        waiting_on_you.append(
                            {
                                "email_id": str(em.id),
                                "sender": em.sender,
                                "subject": em.subject,
                                "priority": str(analysis.priority),
                                "summary": analysis.summary,
                            }
                        )
                        if len(waiting_on_you) >= limit:
                            break
            except Exception as exc:
                logger.debug("radar_waiting_on_you_failed", error=str(exc))

        # Commitments (promises you made) and unresolved followups
        commitments = await self.list_open_commitments(user_id)
        for c in commitments[:limit]:
            waiting_on_others.append(
                {
                    "type": "commitment",
                    "id": str(c.get("id", "")),
                    "label": c.get("promise", ""),
                    "target": c.get("promised_to", "Recipient"),
                    "due_date": str(c.get("due_date") or "Pending"),
                }
            )

        vault = get_token_vault()
        try:
            res = await (
                self.client.table("followups")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("resolved", False)
                .order("due_at", desc=False)
                .limit(limit)
                .execute()
            )
            for item in res.data or []:
                row = dict(cast(dict[str, Any], item))
                waiting_on_others.append(
                    {
                        "type": "followup",
                        "id": str(row.get("id", "")),
                        "email_id": str(row.get("email_id") or ""),
                        "label": vault.decrypt_field(row.get("subject")) or "Follow-up",
                        "target": vault.decrypt_field(row.get("recipient")) or "",
                        "due_date": str(row.get("due_at") or "Pending")[:10],
                    }
                )
        except Exception as exc:
            logger.debug("radar_followups_failed", error=str(exc))

        return {
            "waiting_on_you": waiting_on_you[:limit],
            "waiting_on_others": waiting_on_others[:limit],
        }
