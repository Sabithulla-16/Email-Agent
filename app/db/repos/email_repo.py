"""Email and Analysis repository."""

from typing import Any, cast
from uuid import UUID

from app.db.repos.base import BaseRepo
from app.models.db import (
    EmailAnalysisCreate,
    EmailAnalysisRow,
    EmailCreate,
    EmailListItem,
    EmailRow,
)


class EmailRepo(BaseRepo):
    """Encapsulates email and AI analysis persistence."""

    async def upsert_email(self, email_data: EmailCreate) -> EmailRow:
        payload = email_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("emails")
                .upsert(payload, on_conflict="user_id,gmail_msg_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return EmailRow.model_validate(data[0])

    async def get_by_id(self, email_id: UUID | str) -> EmailRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("emails").select("*").eq("id", str(email_id)).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return EmailRow.model_validate(data[0])

    async def list_by_user(
        self,
        user_id: UUID | str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EmailListItem]:
        """Paginated list of emails.

        Per rule: NEVER select body_clean in list queries to prevent bandwidth bloat.
        """

        async def _query() -> list[dict[str, Any]]:
            columns = (
                "id, user_id, gmail_msg_id, thread_id, sender, sender_email, "
                "subject, received_at, snippet, has_attachments, labels"
            )
            res = await (
                self.client.table("emails")
                .select(columns)
                .eq("user_id", str(user_id))
                .order("received_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [EmailListItem.model_validate(item) for item in data]

    async def upsert_analysis(self, analysis_data: EmailAnalysisCreate) -> EmailAnalysisRow:
        payload = analysis_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("email_analysis")
                .upsert(payload, on_conflict="email_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return EmailAnalysisRow.model_validate(data[0])

    async def get_analysis(self, email_id: UUID | str) -> EmailAnalysisRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("email_analysis")
                .select("*")
                .eq("email_id", str(email_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return EmailAnalysisRow.model_validate(data[0])

    async def match_similar_emails(
        self,
        user_id: UUID | str,
        query_embedding: list[float],
        k: int = 5,
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Perform semantic similarity search via match_emails RPC."""

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.rpc(
                "match_emails",
                {
                    "p_user_id": str(user_id),
                    "p_query_embedding": query_embedding,
                    "p_k": k,
                    "p_threshold": threshold,
                },
            ).execute()
            return cast(list[dict[str, Any]], res.data)

        return await self._execute_with_retry(_query)
