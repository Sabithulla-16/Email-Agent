"""Data retention and privacy cleanup service."""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.db.client import get_supabase

logger = structlog.get_logger("app.services.retention")


class RetentionService:
    """Enforces data retention policies by pruning expired bodies, sessions, and tokens."""

    def __init__(self, client: object = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_supabase()
        return self._client

    @client.setter
    def client(self, val: object) -> None:
        self._client = val

    async def prune_old_email_bodies(self, retention_days: int = 30) -> int:
        """Sanitize email bodies older than retention_days, preserving summaries and metadata."""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        try:
            res = await (
                self.client.table("emails")
                .update({"body_clean": "[RETENTION_PRUNED]"})
                .lt("received_at", cutoff)
                .neq("body_clean", "[RETENTION_PRUNED]")
                .execute()
            )
            count = len(res.data) if res.data else 0
            logger.info("retention_pruned_email_bodies", count=count, cutoff=cutoff)
            return count
        except Exception as exc:
            logger.error("retention_prune_bodies_failed", error=str(exc))
            return 0

    async def prune_expired_tokens_and_sessions(self) -> dict[str, int]:
        """Purge expired temporary tokens, interactions, and revoked sessions."""
        now_iso = datetime.now(UTC).isoformat()
        pruned_stats = {"sessions": 0, "link_tokens": 0, "callback_tokens": 0}

        # 1. Prune expired sessions
        try:
            res = await self.client.table("sessions").delete().lt("expires_at", now_iso).execute()
            pruned_stats["sessions"] = len(res.data) if res.data else 0
        except Exception as exc:
            logger.debug("prune_sessions_error", error=str(exc))

        # 2. Prune expired telegram link tokens
        try:
            res = await (
                self.client.table("telegram_link_tokens")
                .delete()
                .lt("expires_at", now_iso)
                .execute()
            )
            pruned_stats["link_tokens"] = len(res.data) if res.data else 0
        except Exception as exc:
            logger.debug("prune_link_tokens_error", error=str(exc))

        # 3. Prune expired callback tokens
        try:
            res = await (
                self.client.table("callback_tokens").delete().lt("expires_at", now_iso).execute()
            )
            pruned_stats["callback_tokens"] = len(res.data) if res.data else 0
        except Exception as exc:
            logger.debug("prune_callback_tokens_error", error=str(exc))

        logger.info("retention_pruned_tokens", **pruned_stats)
        return pruned_stats
