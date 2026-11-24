"""Follow-up and commitment tracking service."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import structlog

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

        payload = {
            "user_id": str(user_id),
            "email_id": str(email_id) if email_id else None,
            "promise": promise_text,
            "promised_to": "Recipient",
            "due_date": deadline.isoformat(),
            "status": "open",
            "created_at": now.isoformat(),
        }
        try:
            res = await self.client.table("commitments").insert(payload).execute()
            return cast(dict[str, Any], res.data[0]) if res.data else None
        except Exception as exc:
            logger.debug("register_commitment_failed", error=str(exc))
            return None

    async def list_open_commitments(self, user_id: UUID | str) -> list[dict[str, Any]]:
        """List open commitments for a user."""
        try:
            res = await (
                self.client.table("commitments")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("status", "open")
                .order("created_at", desc=True)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])
        except Exception as exc:
            logger.debug("list_commitments_failed", error=str(exc))
            return []
