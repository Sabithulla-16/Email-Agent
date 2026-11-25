"""Smart Actions: meeting extraction, calendar links, task creation, and label suggestions."""

import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import structlog

from app.core.vault import get_token_vault
from app.db.client import get_supabase
from app.models.db import EmailAnalysisRow, EmailRow

logger = structlog.get_logger("app.services.smart_actions")


class SmartActionsService:
    """Provides smart productivity actions for incoming emails."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_supabase()
        return self._client

    def generate_google_calendar_url(
        self,
        title: str,
        start_time: datetime,
        duration_minutes: int = 60,
        details: str = "",
        location: str = "",
    ) -> str:
        """Construct a 1-click Google Calendar add-event URL."""
        end_time = start_time + timedelta(minutes=duration_minutes)
        fmt = "%Y%m%dT%H%M%SZ"
        dates_param = f"{start_time.strftime(fmt)}/{end_time.strftime(fmt)}"

        params = {
            "action": "TEMPLATE",
            "text": title,
            "dates": dates_param,
            "details": details,
            "location": location,
        }
        return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"

    def extract_meeting_details(self, email: EmailRow) -> dict[str, Any] | None:
        """Detect meeting requests in email body and format calendar event data."""
        text = email.body_clean.lower()
        meeting_keywords = ["meet", "zoom", "google meet", "call", "schedule", "sync", "catch up"]

        if not any(k in text for k in meeting_keywords):
            return None

        # Determine reasonable event time (default to tomorrow 14:00 UTC)
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        meeting_time = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, 0, tzinfo=UTC)

        title = f"Sync: {email.subject}"
        cal_url = self.generate_google_calendar_url(
            title=title,
            start_time=meeting_time,
            duration_minutes=30,
            details=f"Context from email:\n{email.snippet or ''}",
        )

        return {
            "has_meeting": True,
            "title": title,
            "suggested_start": meeting_time.isoformat(),
            "calendar_url": cal_url,
        }

    async def create_tasks_from_analysis(
        self, user_id: UUID | str, email_id: UUID | str, analysis: EmailAnalysisRow
    ) -> list[dict[str, Any]]:
        """Persist extracted action items as user tasks in database."""
        created_tasks: list[dict[str, Any]] = []
        action_items = analysis.action_items or []

        for item in action_items:
            task_text = item.get("task") if isinstance(item, dict) else str(item)
            if not task_text:
                continue

            vault = get_token_vault()
            payload = {
                "user_id": str(user_id),
                "email_id": str(email_id),
                "title": vault.encrypt_field(task_text),
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            try:
                res = await self.client.table("tasks").insert(payload).execute()
                if res.data and isinstance(res.data, list):
                    created_tasks.append(cast(dict[str, Any], res.data[0]))
            except Exception as exc:
                logger.debug("create_task_db_error", error=str(exc))

        return created_tasks
