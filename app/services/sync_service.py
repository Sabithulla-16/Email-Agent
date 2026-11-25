"""Email Synchronization Engine: initial lookback, incremental history sync, and deduplication."""

import zoneinfo
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError
from app.db.repos.email_repo import EmailRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import EmailCreate, UserRow
from app.services.auth_service import AuthService
from app.services.email_cleaner import (
    extract_attachments_metadata,
    extract_header_value,
    extract_mime_body,
    is_bulk_or_noreply,
    sanitize_email_body,
)
from app.services.gmail_client import GmailClient, HistoryExpiredError

logger = structlog.get_logger("app.services.sync")

IGNORED_LABELS = {"SENT", "DRAFT", "SPAM", "TRASH"}


class SyncService:
    """Orchestrates per-user email synchronization with Gmail."""

    def __init__(
        self,
        user_repo: UserRepo,
        email_repo: EmailRepo,
        auth_service: AuthService,
        settings: Settings | None = None,
    ) -> None:
        self.user_repo = user_repo
        self.email_repo = email_repo
        self.auth_service = auth_service
        self.settings = settings or get_settings()

    async def sync_user_emails(self, user_id: UUID | str) -> dict[str, Any]:
        """Perform full or incremental sync for the specified user."""
        user = await self.user_repo.get_by_id(user_id)
        if not user or user.deleted_at:
            logger.info("sync_aborted_user_inactive", user_id=str(user_id))
            return {"synced": 0, "status": "user_inactive"}

        # 1. Acquire per-user distributed lock
        acquired = await self.user_repo.acquire_sync_lock(
            user_id, ttl_seconds=self.settings.SYNC_LOCK_TTL_SECONDS
        )
        if not acquired:
            logger.info("sync_skipped_already_locked", user_id=str(user_id))
            raise ConflictError("A sync operation is already in progress for this user.")

        try:
            # 2. Get valid Gmail access token
            token = await self.auth_service.get_valid_access_token(user_id)
            client = GmailClient(access_token=token)

            # 3. Determine if incremental or initial sync
            sync_state = await self.user_repo.get_sync_state(user_id)
            last_history_id = sync_state.last_history_id if sync_state else None

            synced_count = 0
            newest_history_id: str | None = None

            long_disconnect = False
            if last_history_id and sync_state and isinstance(sync_state.last_run_at, datetime):
                last_run = sync_state.last_run_at
                last_run_utc = (
                    last_run.astimezone(UTC) if last_run.tzinfo else last_run.replace(tzinfo=UTC)
                )
                if datetime.now(UTC) - last_run_utc >= timedelta(days=1):
                    long_disconnect = True

            if long_disconnect:
                logger.info("long_disconnect_syncing_today_only", user_id=str(user_id))
                synced_count, newest_history_id = await self._sync_today_only(user, client)
            elif last_history_id:
                try:
                    synced_count, newest_history_id = await self._incremental_sync(
                        user, client, last_history_id
                    )
                except HistoryExpiredError:
                    logger.info("history_id_expired_falling_back_to_initial", user_id=str(user_id))
                    synced_count, newest_history_id = await self._initial_sync(user, client)
            else:
                synced_count, newest_history_id = await self._initial_sync(user, client)

            # 4. Save progress
            await self.user_repo.update_sync_progress(
                user_id=user_id,
                last_history_id=newest_history_id,
            )

            logger.info(
                "sync_completed_successfully",
                user_id=str(user_id),
                synced_count=synced_count,
                history_id=newest_history_id,
            )
            return {
                "synced": synced_count,
                "new_emails_synced": synced_count,
                "history_id": newest_history_id,
                "status": "success",
            }

        except Exception as exc:
            logger.exception("sync_failed", user_id=str(user_id), error=str(exc))
            await self.user_repo.update_sync_progress(user_id=user_id, last_error=str(exc))
            raise
        finally:
            # Always release lock
            await self.user_repo.release_sync_lock(user_id)

    async def _initial_sync(self, user: UserRow, client: GmailClient) -> tuple[int, str | None]:
        """Fetch emails from the last N days (default 3)."""
        lookback_days = self.settings.SYNC_LOOKBACK_DAYS
        cutoff_date = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
        query = f"after:{cutoff_date} -label:DRAFT -label:SPAM -label:TRASH"

        logger.info("running_initial_sync", user_id=str(user.id), query=query)
        msg_list_resp = await client.list_messages(query=query, max_results=100)
        messages = msg_list_resp.get("messages", [])

        synced = 0
        newest_history_id: str | None = None

        for m in messages:
            msg_id = m.get("id")
            if not msg_id:
                continue
            history_id = await self._process_and_store_message(user, client, msg_id)
            if history_id:
                synced += 1
                newest_history_id = max(
                    newest_history_id or "0",
                    history_id,
                    key=lambda x: int(x) if x.isdigit() else 0,
                )

        return synced, newest_history_id

    async def _sync_today_only(self, user: UserRow, client: GmailClient) -> tuple[int, str | None]:
        """Fetch only emails received on the current local day (after long disconnect)."""
        user_tz = user.timezone if user.timezone and user.timezone != "UTC" else "Asia/Kolkata"
        try:
            tz: Any = zoneinfo.ZoneInfo(user_tz)
        except Exception:
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        today_str = datetime.now(tz).strftime("%Y/%m/%d")
        query = f"after:{today_str} -label:DRAFT -label:SPAM -label:TRASH"

        logger.info("running_today_only_sync", user_id=str(user.id), query=query)
        msg_list_resp = await client.list_messages(query=query, max_results=100)
        messages = msg_list_resp.get("messages", [])

        synced = 0
        newest_history_id: str | None = None
        for m in messages:
            msg_id = m.get("id")
            if not msg_id:
                continue
            history_id = await self._process_and_store_message(user, client, msg_id)
            if history_id:
                synced += 1
                newest_history_id = max(
                    newest_history_id or "0",
                    history_id,
                    key=lambda x: int(x) if x.isdigit() else 0,
                )

        return synced, newest_history_id

    async def _incremental_sync(
        self, user: UserRow, client: GmailClient, start_history_id: str
    ) -> tuple[int, str | None]:
        """Fetch incremental changes from Gmail history."""
        logger.info(
            "running_incremental_sync",
            user_id=str(user.id),
            start_history_id=start_history_id,
        )
        history_resp = await client.list_history(start_history_id=start_history_id, max_results=100)
        histories = history_resp.get("history", [])

        synced = 0
        latest_history_id: str = history_resp.get("historyId", start_history_id)

        msg_ids_to_fetch: set[str] = set()
        for h in histories:
            messages_added = h.get("messagesAdded", [])
            for item in messages_added:
                msg = item.get("message")
                if msg and msg.get("id"):
                    msg_ids_to_fetch.add(msg["id"])

        for msg_id in msg_ids_to_fetch:
            h_id = await self._process_and_store_message(user, client, msg_id)
            if h_id:
                synced += 1
                latest_history_id = max(
                    latest_history_id,
                    h_id,
                    key=lambda x: int(x) if x.isdigit() else 0,
                )

        return synced, latest_history_id

    async def _process_and_store_message(
        self, user: UserRow, client: GmailClient, message_id: str
    ) -> str | None:
        """Fetch, sanitize, and persist a single Gmail message.

        Isolates failures so a single malformed message does not fail the batch.
        """
        try:
            raw_msg = await client.get_message(message_id)
            history_id = raw_msg.get("historyId")
            label_ids = raw_msg.get("labelIds", [])

            # Skip ignored labels
            if any(lbl in IGNORED_LABELS for lbl in label_ids):
                return history_id

            payload = raw_msg.get("payload", {})
            headers = payload.get("headers", [])

            # Extract header values
            from_header = extract_header_value(headers, "From") or "Unknown"
            reply_to_header = extract_header_value(headers, "Reply-To")
            subject_header = extract_header_value(headers, "Subject") or "(No Subject)"
            date_header = extract_header_value(headers, "Date")
            rfc_msg_id = extract_header_value(headers, "Message-ID")
            list_unsub = extract_header_value(headers, "List-Unsubscribe")
            precedence = extract_header_value(headers, "Precedence")

            sender_name, sender_email = parseaddr(from_header)
            sender_display = sender_name or sender_email

            # Skip emails sent by the user themselves
            if sender_email.lower() == user.email.lower():
                return history_id

            # Parse received timestamp
            if date_header:
                try:
                    received_at = parsedate_to_datetime(date_header)
                except Exception:
                    received_at = datetime.now(UTC)
            else:
                received_at = datetime.now(UTC)

            # MIME parsing & cleaning
            raw_body, is_html = extract_mime_body(payload)
            body_clean = sanitize_email_body(
                raw_body, is_html, max_chars=self.settings.EMAIL_BODY_MAX_CHARS
            )

            # Attachment metadata
            attachments = extract_attachments_metadata(payload)
            has_attachments = len(attachments) > 0

            # Bulk / automated email detection
            is_bulk = is_bulk_or_noreply(from_header, list_unsub, precedence)
            labels = list(label_ids)
            if is_bulk and "BULK" not in labels:
                labels.append("BULK")

            # Upsert into database
            await self.email_repo.upsert_email(
                EmailCreate(
                    user_id=str(user.id),
                    gmail_msg_id=message_id,
                    thread_id=raw_msg.get("threadId", message_id),
                    rfc_message_id=rfc_msg_id,
                    sender=sender_display,
                    sender_email=sender_email,
                    reply_to=reply_to_header,
                    subject=subject_header,
                    received_at=received_at.isoformat(),
                    snippet=raw_msg.get("snippet"),
                    body_clean=body_clean,
                    has_attachments=has_attachments,
                    list_unsubscribe=list_unsub,
                    labels=labels,
                )
            )

            return history_id

        except Exception as exc:
            logger.warning("failed_to_process_single_email", msg_id=message_id, error=str(exc))
            return None
