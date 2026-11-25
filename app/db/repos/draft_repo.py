"""Draft and Draft Versions repository."""

from typing import Any, cast
from uuid import UUID

from app.core.errors import ConflictError, NotFoundError
from app.core.vault import TokenVault, get_token_vault
from app.db.repos.base import BaseRepo
from app.models.db import (
    DraftCreate,
    DraftRow,
    DraftVersionRow,
)


class DraftRepo(BaseRepo):
    """Encapsulates draft generation, versioning, and state transitions with encrypted bodies."""

    def __init__(self, client: Any, vault: TokenVault | None = None) -> None:
        super().__init__(client)
        self._vault = vault

    @property
    def vault(self) -> TokenVault:
        if self._vault is None:
            self._vault = get_token_vault()
        return self._vault

    def _decrypt_version_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decrypted = dict(row)
        for field in ("body", "user_instruction"):
            if field in decrypted and isinstance(decrypted[field], str):
                decrypted[field] = self.vault.decrypt_field(decrypted[field])
        return decrypted

    async def create_draft(self, draft_data: DraftCreate) -> DraftRow:
        payload = draft_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("drafts").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return DraftRow.model_validate(data[0])

    async def get_by_id(self, draft_id: UUID | str) -> DraftRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("drafts").select("*").eq("id", str(draft_id)).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return DraftRow.model_validate(data[0])

    async def get_by_email_id(self, email_id: UUID | str) -> DraftRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("drafts")
                .select("*")
                .eq("email_id", str(email_id))
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return DraftRow.model_validate(data[0])

    async def list_by_user(
        self,
        user_id: UUID | str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DraftRow]:
        async def _query() -> list[dict[str, Any]]:
            builder = self.client.table("drafts").select("*").eq("user_id", str(user_id))
            if status:
                builder = builder.eq("status", status)
            res = await (
                builder.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [DraftRow.model_validate(item) for item in data]

    async def transition_draft(
        self,
        draft_id: UUID | str,
        from_status: str,
        to_status: str,
        failure_reason: str | None = None,
        sent_gmail_msg_id: str | None = None,
    ) -> DraftRow:
        """Atomic Compare-And-Set transition with audit logging via SQL function."""

        async def _query() -> dict[str, Any] | None:
            res = await self.client.rpc(
                "transition_draft",
                {
                    "p_draft_id": str(draft_id),
                    "p_from_status": from_status,
                    "p_to_status": to_status,
                    "p_failure_reason": failure_reason,
                    "p_sent_gmail_msg_id": sent_gmail_msg_id,
                },
            ).execute()
            return cast(dict[str, Any] | None, res.data)

        data = await self._execute_with_retry(_query)
        if not data or not data.get("id"):
            raise ConflictError(
                f"Draft {draft_id} cannot transition from {from_status} to {to_status}. "
                "Current draft status does not match expected state."
            )
        return DraftRow.model_validate(data)

    async def create_version(
        self,
        draft_id: UUID | str,
        body: str,
        user_instruction: str | None = None,
        source: str = "llm",
        model: str = "llama-3.3-70b-versatile",
    ) -> DraftVersionRow:
        """Atomically increments version and inserts an encrypted draft version record."""
        enc_body = self.vault.encrypt_field(body) or ""
        enc_instruction = (
            self.vault.encrypt_field(user_instruction) if user_instruction else user_instruction
        )

        async def _query() -> dict[str, Any]:
            res = await self.client.rpc(
                "create_draft_version",
                {
                    "p_draft_id": str(draft_id),
                    "p_body": enc_body,
                    "p_user_instruction": enc_instruction,
                    "p_source": source,
                    "p_model": model,
                },
            ).execute()
            return cast(dict[str, Any], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            raise NotFoundError(f"Draft {draft_id} not found to increment version")
        return DraftVersionRow.model_validate(self._decrypt_version_row(data))

    async def get_versions(self, draft_id: UUID | str) -> list[DraftVersionRow]:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("draft_versions")
                .select("*")
                .eq("draft_id", str(draft_id))
                .order("version", desc=False)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [DraftVersionRow.model_validate(self._decrypt_version_row(item)) for item in data]
