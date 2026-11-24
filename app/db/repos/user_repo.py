"""User, SyncState, and Preferences repository."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.core.errors import NotFoundError
from app.db.repos.base import BaseRepo
from app.models.db import (
    NotificationPrefsRow,
    OAuthTokenRow,
    OAuthTokenUpsert,
    SessionCreate,
    SessionRow,
    SyncStateRow,
    UserCreate,
    UserRow,
    UserUpdate,
)


class UserRepo(BaseRepo):
    """Encapsulates all user, session, and credential data access."""

    async def get_by_id(self, user_id: UUID | str) -> UserRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("users").select("*").eq("id", str(user_id)).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return UserRow.model_validate(data[0])

    async def get_by_google_sub(self, google_sub: str) -> UserRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("users").select("*").eq("google_sub", google_sub).execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return UserRow.model_validate(data[0])

    async def upsert_user(self, user_data: UserCreate) -> UserRow:
        payload = user_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("users").upsert(payload, on_conflict="google_sub").execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return UserRow.model_validate(data[0])

    async def update_user(self, user_id: UUID | str, update_data: UserUpdate) -> UserRow:
        payload = update_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("users").update(payload).eq("id", str(user_id)).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            raise NotFoundError(f"User {user_id} not found")
        return UserRow.model_validate(data[0])

    async def claim_due_users(self, limit: int = 20) -> list[UserRow]:
        """Claim users due for digest using atomic stored procedure."""

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.rpc("claim_due_users", {"p_limit": limit}).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return [UserRow.model_validate(row) for row in data]

    async def delete_user_data(self, user_id: UUID | str) -> None:
        """Call atomic delete_user_data RPC."""

        async def _query() -> None:
            await self.client.rpc("delete_user_data", {"p_user_id": str(user_id)}).execute()

        await self._execute_with_retry(_query)

    # -------------------------------------------------------------------------
    # OAuth Tokens
    # -------------------------------------------------------------------------
    async def get_oauth_token(self, user_id: UUID | str) -> OAuthTokenRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("oauth_tokens")
                .select("*")
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return OAuthTokenRow.model_validate(data[0])

    async def upsert_oauth_token(self, token_data: OAuthTokenUpsert) -> OAuthTokenRow:
        payload = token_data.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("oauth_tokens")
                .upsert(payload, on_conflict="user_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return OAuthTokenRow.model_validate(data[0])

    async def update_token_status(self, user_id: UUID | str, status: str) -> None:
        async def _query() -> None:
            await (
                self.client.table("oauth_tokens")
                .update({"status": status})
                .eq("user_id", str(user_id))
                .execute()
            )

        await self._execute_with_retry(_query)

    # -------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------
    async def create_session(self, session: SessionCreate) -> SessionRow:
        payload = session.model_dump(exclude_none=True)

        async def _query() -> list[dict[str, Any]]:
            res = await self.client.table("sessions").insert(payload).execute()
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return SessionRow.model_validate(data[0])

    async def get_valid_session(self, token_hash: str) -> SessionRow | None:
        now_iso = datetime.now(UTC).isoformat()

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("sessions")
                .select("*")
                .eq("token_hash", token_hash)
                .is_("revoked_at", "null")
                .gt("expires_at", now_iso)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return SessionRow.model_validate(data[0])

    async def revoke_session(self, token_hash: str) -> bool:
        now_iso = datetime.now(UTC).isoformat()

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("sessions")
                .update({"revoked_at": now_iso})
                .eq("token_hash", token_hash)
                .is_("revoked_at", "null")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return len(data) == 1

    # -------------------------------------------------------------------------
    # Sync State
    # -------------------------------------------------------------------------
    async def get_sync_state(self, user_id: UUID | str) -> SyncStateRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("sync_state")
                .select("*")
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return SyncStateRow.model_validate(data[0])

    async def acquire_sync_lock(self, user_id: UUID | str, ttl_seconds: int = 300) -> bool:
        async def _query() -> bool:
            res = await self.client.rpc(
                "acquire_sync_lock",
                {"p_user_id": str(user_id), "p_ttl_seconds": ttl_seconds},
            ).execute()
            return bool(res.data)

        return await self._execute_with_retry(_query)

    async def release_sync_lock(self, user_id: UUID | str) -> None:
        async def _query() -> None:
            await self.client.rpc("release_sync_lock", {"p_user_id": str(user_id)}).execute()

        await self._execute_with_retry(_query)

    async def update_sync_progress(
        self,
        user_id: UUID | str,
        last_history_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"last_run_at": datetime.now(UTC).isoformat()}
        if last_history_id is not None:
            payload["last_history_id"] = last_history_id
        if last_error is not None:
            payload["last_error"] = last_error

        async def _query() -> None:
            await (
                self.client.table("sync_state")
                .upsert(
                    {"user_id": str(user_id), **payload},
                    on_conflict="user_id",
                )
                .execute()
            )

        await self._execute_with_retry(_query)

    # -------------------------------------------------------------------------
    # Notification Preferences
    # -------------------------------------------------------------------------
    async def get_notification_prefs(self, user_id: UUID | str) -> NotificationPrefsRow | None:
        async def _query() -> list[dict[str, Any]]:
            res = (
                await self.client.table("notification_prefs")
                .select("*")
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        if not data:
            return None
        return NotificationPrefsRow.model_validate(data[0])

    async def upsert_notification_prefs(
        self, user_id: UUID | str, prefs: dict[str, Any]
    ) -> NotificationPrefsRow:
        payload = {"user_id": str(user_id), **prefs}

        async def _query() -> list[dict[str, Any]]:
            res = await (
                self.client.table("notification_prefs")
                .upsert(payload, on_conflict="user_id")
                .execute()
            )
            return cast(list[dict[str, Any]], res.data)

        data = await self._execute_with_retry(_query)
        return NotificationPrefsRow.model_validate(data[0])
