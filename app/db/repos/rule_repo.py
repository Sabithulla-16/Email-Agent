"""Rules, Feedback, and Audit Log repository."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.core.errors import NotFoundError
from app.db.repos.base import BaseRepo
from app.models.db import (
    AuditLogRow,
    FeedbackCreate,
    FeedbackRow,
    RuleCreate,
    RuleRow,
    RuleUpdate,
)


class RuleRepo(BaseRepo):
    """Encapsulates user automation rules, feedback submissions, and system audit logs."""

    # -------------------------------------------------------------------------
    # Rules
    # -------------------------------------------------------------------------
    async def list_by_user(self, user_id: UUID | str) -> list[RuleRow]:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("rules")
                .select("*")
                .eq("user_id", str(user_id))
                .order("created_at", desc=False)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [RuleRow.model_validate(item) for item in data]

    async def create_rule(self, rule_data: RuleCreate) -> RuleRow:
        payload = rule_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("rules").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return RuleRow.model_validate(data[0])

    async def update_rule(
        self, rule_id: UUID | str, user_id: UUID | str, update_data: RuleUpdate
    ) -> RuleRow:
        payload = update_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("rules")
                .update(payload)
                .eq("id", str(rule_id))
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            raise NotFoundError(f"Rule {rule_id} not found")
        return RuleRow.model_validate(data[0])

    async def delete_rule(self, rule_id: UUID | str, user_id: UUID | str) -> bool:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("rules")
                .delete()
                .eq("id", str(rule_id))
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return len(data) > 0

    # -------------------------------------------------------------------------
    # Feedback
    # -------------------------------------------------------------------------
    async def create_feedback(self, feedback: FeedbackCreate) -> FeedbackRow:
        payload = feedback.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("feedback").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return FeedbackRow.model_validate(data[0])

    # -------------------------------------------------------------------------
    # Audit Log
    # -------------------------------------------------------------------------
    async def log_event(
        self,
        action: str,
        entity: str,
        entity_id: str | None = None,
        user_id: UUID | str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AuditLogRow:
        payload = {
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "user_id": str(user_id) if user_id else None,
            "meta": meta or {},
            "ts": datetime.now(UTC).isoformat(),
        }

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("audit_log").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return AuditLogRow.model_validate(data[0])
