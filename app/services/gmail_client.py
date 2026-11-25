"""Gmail API client running synchronous google-api-python-client calls in asyncio.to_thread."""

import asyncio
from typing import Any, cast

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from app.core.errors import ExternalServiceError, NotFoundError

logger = structlog.get_logger("app.services.gmail_client")


class HistoryExpiredError(Exception):
    """Raised when Gmail history ID is outdated and returns 404."""


class GmailClient:
    """Async wrapper around Google Gmail API client."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self._service: Resource | None = None

    def _get_service(self) -> Resource:
        if self._service is None:
            creds = Credentials(token=self.access_token)  # type: ignore[no-untyped-call]
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    async def list_messages(
        self,
        query: str = "",
        max_results: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch list of message metadata matching query."""

        def _sync_list() -> dict[str, Any]:
            service = self._get_service()
            params: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(max_results, 100),
                "q": query,
            }
            if page_token:
                params["pageToken"] = page_token
            return cast(dict[str, Any], service.users().messages().list(**params).execute())

        try:
            return await asyncio.to_thread(_sync_list)
        except HttpError as err:
            logger.error("gmail_list_messages_failed", error=str(err))
            raise ExternalServiceError(f"Gmail list messages failed: {err}") from err

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Fetch full MIME structure of a single email message."""

        def _sync_get() -> dict[str, Any]:
            service = self._get_service()
            res = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )
            return cast(dict[str, Any], res)

        try:
            return await asyncio.to_thread(_sync_get)
        except HttpError as err:
            if err.resp.status == 404:
                raise NotFoundError(f"Gmail message {message_id} not found") from err
            logger.error("gmail_get_message_failed", msg_id=message_id, error=str(err))
            raise ExternalServiceError(f"Gmail get message failed: {err}") from err

    async def list_history(
        self,
        start_history_id: str,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Perform incremental sync via Gmail history.list.

        Raises HistoryExpiredError if the history ID is older than ~30 days (404).
        """

        def _sync_history() -> dict[str, Any]:
            service = self._get_service()
            res = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    maxResults=max_results,
                    historyTypes=["messageAdded"],
                )
                .execute()
            )
            return cast(dict[str, Any], res)

        try:
            return await asyncio.to_thread(_sync_history)
        except HttpError as err:
            if err.resp.status == 404:
                logger.info("gmail_history_id_expired", start_history_id=start_history_id)
                raise HistoryExpiredError("Gmail history ID expired") from err
            logger.error("gmail_history_list_failed", error=str(err))
            raise ExternalServiceError(f"Gmail history sync failed: {err}") from err

    async def send_raw_message(self, raw_b64: str, thread_id: str | None = None) -> dict[str, Any]:
        """Send an RFC 2822 base64url encoded MIME message."""

        def _sync_send() -> dict[str, Any]:
            service = self._get_service()
            body: dict[str, Any] = {"raw": raw_b64}
            if thread_id:
                body["threadId"] = thread_id
            return cast(
                dict[str, Any],
                service.users().messages().send(userId="me", body=body).execute(),
            )

        try:
            return await asyncio.to_thread(_sync_send)
        except HttpError as err:
            logger.error("gmail_send_message_failed", error=str(err))
            raise ExternalServiceError(f"Gmail message send failed: {err}") from err

    async def archive_message(self, message_id: str) -> dict[str, Any]:
        """Remove INBOX label from a Gmail message to archive it."""

        def _sync_archive() -> dict[str, Any]:
            service = self._get_service()
            return cast(
                dict[str, Any],
                service.users()
                .messages()
                .modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]})
                .execute(),
            )

        try:
            return await asyncio.to_thread(_sync_archive)
        except HttpError as err:
            logger.error("gmail_archive_message_failed", msg_id=message_id, error=str(err))
            raise ExternalServiceError(f"Gmail archive message failed: {err}") from err
