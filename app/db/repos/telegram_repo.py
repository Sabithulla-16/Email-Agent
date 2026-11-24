"""Telegram bot link, token, callback, and interaction state repository."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.db.repos.base import BaseRepo
from app.models.db import (
    CallbackTokenRow,
    PendingInteractionRow,
    TelegramLinkRow,
    TelegramLinkTokenRow,
)


class TelegramRepo(BaseRepo):
    """Encapsulates Telegram bot integration tables and single-use token consumption."""

    # -------------------------------------------------------------------------
    # Telegram Links
    # -------------------------------------------------------------------------
    async def get_link_by_chat_id(self, chat_id: int) -> TelegramLinkRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("telegram_links")
                .select("*")
                .eq("chat_id", chat_id)
                .eq("is_active", True)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return TelegramLinkRow.model_validate(data[0])

    async def get_link_by_user_id(self, user_id: UUID | str) -> TelegramLinkRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("telegram_links")
                .select("*")
                .eq("user_id", str(user_id))
                .eq("is_active", True)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return TelegramLinkRow.model_validate(data[0])

    async def link_chat(self, user_id: UUID | str, chat_id: int) -> TelegramLinkRow:
        payload = {
            "user_id": str(user_id),
            "chat_id": chat_id,
            "is_active": True,
            "linked_at": datetime.now(UTC).isoformat(),
        }

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("telegram_links").upsert(payload, on_conflict="chat_id").execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return TelegramLinkRow.model_validate(data[0])

    async def deactivate_chat(self, chat_id: int) -> None:
        """Called when bot is blocked (HTTP 403) or user runs /disconnect."""

        async def _query() -> None:
            await (
                self.client.table("telegram_links")
                .update({"is_active": False})
                .eq("chat_id", chat_id)
                .execute()
            )

        await self._execute_with_retry(_query)

    # -------------------------------------------------------------------------
    # Single-use Deep Link Tokens
    # -------------------------------------------------------------------------
    async def create_link_token(
        self, token_hash: str, user_id: UUID | str, expires_at: datetime
    ) -> TelegramLinkTokenRow:
        payload = {
            "token_hash": token_hash,
            "user_id": str(user_id),
            "expires_at": expires_at.isoformat(),
        }

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("telegram_link_tokens").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return TelegramLinkTokenRow.model_validate(data[0])

    async def consume_link_token(self, token_hash: str) -> UUID | None:
        """Atomic compare-and-set: set used_at WHERE used_at IS NULL AND expires_at > now().

        Succeeds only if exactly one row was updated. Returns user_id if valid.
        """
        now_iso = datetime.now(UTC).isoformat()

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("telegram_link_tokens")
                .update({"used_at": now_iso})
                .eq("token_hash", token_hash)
                .is_("used_at", "null")
                .gt("expires_at", now_iso)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if len(data) != 1:
            return None
        return UUID(data[0]["user_id"])

    # -------------------------------------------------------------------------
    # Single-use Inline Button Callback Tokens
    # -------------------------------------------------------------------------
    async def create_callback_token(
        self,
        token: str,
        user_id: UUID | str,
        chat_id: int,
        draft_id: UUID | str,
        action: str,
        expires_at: datetime,
    ) -> CallbackTokenRow:
        payload = {
            "token": token,
            "user_id": str(user_id),
            "chat_id": chat_id,
            "draft_id": str(draft_id),
            "action": action,
            "expires_at": expires_at.isoformat(),
        }

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("callback_tokens").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return CallbackTokenRow.model_validate(data[0])

    async def consume_callback_token(self, token: str, chat_id: int) -> CallbackTokenRow | None:
        """Atomic single-use token consumption.

        Matches token + chat_id, ensures used_at IS NULL and expires_at > now().
        """
        now_iso = datetime.now(UTC).isoformat()

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("callback_tokens")
                .update({"used_at": now_iso})
                .eq("token", token)
                .eq("chat_id", chat_id)
                .is_("used_at", "null")
                .gt("expires_at", now_iso)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if len(data) != 1:
            return None
        return CallbackTokenRow.model_validate(data[0])

    # -------------------------------------------------------------------------
    # Pending Interactions (ForceReply state)
    # -------------------------------------------------------------------------
    async def set_pending_interaction(
        self,
        chat_id: int,
        user_id: UUID | str,
        draft_id: UUID | str,
        interaction_type: str,
        expires_at: datetime,
    ) -> PendingInteractionRow:
        payload = {
            "chat_id": chat_id,
            "user_id": str(user_id),
            "draft_id": str(draft_id),
            "type": interaction_type,
            "expires_at": expires_at.isoformat(),
        }

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("pending_interactions")
                .upsert(payload, on_conflict="chat_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return PendingInteractionRow.model_validate(data[0])

    async def get_and_clear_pending_interaction(self, chat_id: int) -> PendingInteractionRow | None:
        now_iso = datetime.now(UTC).isoformat()

        async def _query_select() -> list[dict[str, Any]]:
            res = await (
                self.client.table("pending_interactions")
                .select("*")
                .eq("chat_id", chat_id)
                .gt("expires_at", now_iso)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query_select)
        if not data:
            return None

        # Delete immediately so it cannot be reused
        async def _query_delete() -> None:
            await (
                self.client.table("pending_interactions").delete().eq("chat_id", chat_id).execute()
            )

        await self._execute_with_retry(_query_delete)
        return PendingInteractionRow.model_validate(data[0])
