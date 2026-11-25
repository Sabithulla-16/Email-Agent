"""Email and Analysis repository."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from app.core.vault import TokenVault, get_token_vault
from app.db.repos.base import BaseRepo
from app.models.db import (
    EmailAnalysisCreate,
    EmailAnalysisRow,
    EmailCreate,
    EmailListItem,
    EmailRow,
)

_ENCRYPTED_EMAIL_FIELDS = (
    "sender",
    "sender_email",
    "reply_to",
    "subject",
    "snippet",
    "body_clean",
    "list_unsubscribe",
)


class EmailRepo(BaseRepo):
    """Encapsulates email and AI analysis persistence with field-level AES-256-GCM encryption."""

    def __init__(self, client: Any, vault: TokenVault | None = None) -> None:
        super().__init__(client)
        self._vault = vault

    @property
    def vault(self) -> TokenVault:
        if self._vault is None:
            self._vault = get_token_vault()
        return self._vault

    def _encrypt_email_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        encrypted = dict(payload)
        for field in _ENCRYPTED_EMAIL_FIELDS:
            if field in encrypted and isinstance(encrypted[field], str):
                encrypted[field] = self.vault.encrypt_field(encrypted[field])
        return encrypted

    def _decrypt_email_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decrypted = dict(row)
        for field in _ENCRYPTED_EMAIL_FIELDS:
            if field in decrypted and isinstance(decrypted[field], str):
                decrypted[field] = self.vault.decrypt_field(decrypted[field])
        return decrypted

    def _encrypt_analysis_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        encrypted = dict(payload)
        if "summary" in encrypted and isinstance(encrypted["summary"], str):
            encrypted["summary"] = self.vault.encrypt_field(encrypted["summary"])
        if "action_items" in encrypted and encrypted["action_items"] is not None:
            encrypted["action_items"] = self.vault.encrypt_json_strings(encrypted["action_items"])
        return encrypted

    def _decrypt_analysis_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decrypted = dict(row)
        if "summary" in decrypted and isinstance(decrypted["summary"], str):
            decrypted["summary"] = self.vault.decrypt_field(decrypted["summary"])
        if "action_items" in decrypted and decrypted["action_items"] is not None:
            decrypted["action_items"] = self.vault.decrypt_json_strings(decrypted["action_items"])
        return decrypted

    async def upsert_email(self, email_data: EmailCreate) -> EmailRow:
        payload = self._encrypt_email_payload(email_data.model_dump(exclude_none=True))

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("emails")
                .upsert(payload, on_conflict="user_id,gmail_msg_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return EmailRow.model_validate(self._decrypt_email_row(data[0]))

    async def get_by_id(self, email_id: UUID | str) -> EmailRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("emails").select("*").eq("id", str(email_id)).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return EmailRow.model_validate(self._decrypt_email_row(data[0]))

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
        return [EmailListItem.model_validate(self._decrypt_email_row(item)) for item in data]

    async def list_by_user_since(
        self,
        user_id: UUID | str,
        since: datetime,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[EmailListItem]:
        """List emails received strictly after `since` (and optionally up to `until`)."""

        async def _query() -> list[dict[str, Any]]:
            columns = (
                "id, user_id, gmail_msg_id, thread_id, sender, sender_email, "
                "subject, received_at, snippet, has_attachments, labels"
            )
            builder = (
                self.client.table("emails")
                .select(columns)
                .eq("user_id", str(user_id))
                .gt("received_at", since.isoformat())
            )
            if until is not None:
                builder = builder.lte("received_at", until.isoformat())
            res = await builder.order("received_at", desc=True).limit(limit).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [EmailListItem.model_validate(self._decrypt_email_row(item)) for item in data]

    async def upsert_analysis(self, analysis_data: EmailAnalysisCreate) -> EmailAnalysisRow:
        payload = self._encrypt_analysis_payload(analysis_data.model_dump(exclude_none=True))

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("email_analysis")
                .upsert(payload, on_conflict="email_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return EmailAnalysisRow.model_validate(self._decrypt_analysis_row(data[0]))

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
        return EmailAnalysisRow.model_validate(self._decrypt_analysis_row(data[0]))

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

        data = await self._execute_with_retry(_query)
        return [
            {
                **row,
                "subject": self.vault.decrypt_field(row.get("subject")),
                "snippet": self.vault.decrypt_field(row.get("snippet")),
            }
            for row in data
        ]

    async def list_newsletters_by_user(
        self,
        user_id: UUID | str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """List recent emails for user_id that contain a List-Unsubscribe header."""

        async def _query() -> list[dict[str, Any]]:
            columns = (
                "id, user_id, gmail_msg_id, sender, sender_email, "
                "subject, received_at, list_unsubscribe, labels"
            )
            res = await (
                self.client.table("emails")
                .select(columns)
                .eq("user_id", str(user_id))
                .not_.is_("list_unsubscribe", "null")
                .order("received_at", desc=True)
                .limit(limit)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [self._decrypt_email_row(item) for item in data]

    async def clear_list_unsubscribe(
        self, user_id: UUID | str, gmail_msg_ids: list[str]
    ) -> None:
        """Clear list_unsubscribe on archived/unsubscribed emails so they leave /unsubscribe."""
        if not gmail_msg_ids:
            return

        async def _query() -> None:
            await (
                self.client.table("emails")
                .update({"list_unsubscribe": None})
                .eq("user_id", str(user_id))
                .in_("gmail_msg_id", gmail_msg_ids)
                .execute()
            )

        await self._execute_with_retry(_query)

    async def search_decrypted(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 5,
        scan_limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search user's decrypted recent emails by query tokens and cached summaries."""
        clean_q = query.strip().lower()
        if not clean_q:
            return []

        tokens = [t for t in clean_q.split() if len(t) >= 2] or [clean_q]
        emails = await self.list_by_user(user_id=user_id, limit=scan_limit)
        scored: list[tuple[float, dict[str, Any]]] = []

        for em in emails:
            summary_text = ""
            try:
                analysis = await self.get_analysis(em.id)
                if analysis and analysis.summary:
                    summary_text = analysis.summary
            except Exception:
                pass

            haystack = " ".join(
                [
                    em.sender or "",
                    em.sender_email or "",
                    em.subject or "",
                    em.snippet or "",
                    summary_text,
                ]
            ).lower()

            score = 0.0
            if clean_q in haystack:
                score += 5.0
            for tok in tokens:
                if tok in (em.subject or "").lower():
                    score += 2.0
                elif tok in (em.sender or "").lower() or tok in (em.sender_email or "").lower():
                    score += 1.5
                elif tok in haystack:
                    score += 1.0

            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "id": str(em.id),
                            "gmail_msg_id": em.gmail_msg_id,
                            "sender": em.sender,
                            "sender_email": em.sender_email,
                            "subject": em.subject,
                            "received_at": em.received_at.isoformat() if em.received_at else "",
                            "summary": summary_text or (em.snippet or "")[:180],
                            "score": score,
                        },
                    )
                )

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]
