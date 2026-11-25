"""One-tap newsletter & promo unsubscribe + Gmail archive service."""

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
import structlog

from app.db.repos.email_repo import EmailRepo
from app.services.auth_service import AuthService
from app.services.gmail_client import GmailClient
from app.services.mime_builder import build_mime_reply

logger = structlog.get_logger("app.services.unsubscribe")

_URL_RE = re.compile(r"<(https?://[^>]+)>", re.IGNORECASE)
_MAILTO_RE = re.compile(r"<(mailto:[^>]+)>", re.IGNORECASE)


def parse_list_unsubscribe(header_val: str | None) -> tuple[str | None, str | None]:
    """Extract (https_url, mailto_addr) from an RFC 2369 List-Unsubscribe header."""
    if not header_val:
        return None, None
    url_match = _URL_RE.search(header_val)
    mailto_match = _MAILTO_RE.search(header_val)
    https_url = url_match.group(1).strip() if url_match else None
    if not https_url and header_val.strip().lower().startswith("https://"):
        https_url = header_val.strip()
    mailto_val = mailto_match.group(1).strip() if mailto_match else None
    return https_url, mailto_val


def is_safe_public_url(url: str) -> bool:
    """Validate that URL is HTTPS and does not resolve to private/loopback IPs (SSRF guard)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_reserved
                or ip_obj.is_multicast
            ):
                return False
        return True
    except Exception:
        return False


class UnsubscribeService:
    """Per-user newsletter detection, safe one-click unsubscribe, and Gmail archiving."""

    def __init__(self, email_repo: EmailRepo, auth_service: AuthService) -> None:
        self.email_repo = email_repo
        self.auth_service = auth_service

    async def list_candidates(self, user_id: UUID | str, limit: int = 5) -> list[dict[str, Any]]:
        """Group recent unsubscribable emails by sender_email for this user."""
        rows = await self.email_repo.list_newsletters_by_user(user_id=user_id, limit=40)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            sender_email = (row.get("sender_email") or "").lower().strip()
            if not sender_email:
                continue
            https_url, mailto_val = parse_list_unsubscribe(row.get("list_unsubscribe"))
            if not https_url and not mailto_val:
                continue
            if sender_email not in grouped:
                grouped[sender_email] = {
                    "email_id": str(row["id"]),
                    "sender": row.get("sender") or sender_email,
                    "sender_email": sender_email,
                    "https_url": https_url,
                    "mailto": mailto_val,
                    "gmail_msg_ids": [],
                }
            grouped[sender_email]["gmail_msg_ids"].append(row["gmail_msg_id"])
        return list(grouped.values())[:limit]

    async def unsubscribe_and_archive(
        self, user_id: UUID | str, email_id: UUID | str
    ) -> dict[str, Any]:
        """Execute one-click unsubscribe and archive matching inbox emails for user_id."""
        email = await self.email_repo.get_by_id(email_id)
        if not email or str(email.user_id) != str(user_id):
            return {"status": "not_found", "unsubscribed": False, "archived_count": 0}

        https_url, mailto_val = parse_list_unsubscribe(email.list_unsubscribe)
        unsub_method = "none"

        if https_url and is_safe_public_url(https_url):
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                    current_url = https_url
                    resp: httpx.Response | None = None
                    for _ in range(4):
                        resp = await client.post(
                            current_url,
                            data={"List-Unsubscribe": "One-Click"},
                        )
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = resp.headers.get("location", "")
                            next_url = urljoin(current_url, loc)
                            if not is_safe_public_url(next_url):
                                logger.warning("ssrf_redirect_blocked", target=next_url)
                                resp = None
                                break
                            current_url = next_url
                            continue
                        break
                    if resp is not None and resp.status_code >= 400:
                        resp = await client.get(current_url)
                    if resp is not None and resp.status_code < 400:
                        unsub_method = "https"
            except Exception as exc:
                logger.warning("https_unsubscribe_failed", error=str(exc))

        access_token = await self.auth_service.get_valid_access_token(user_id)
        gmail = GmailClient(access_token=access_token)

        if unsub_method == "none" and mailto_val:
            try:
                target_addr = mailto_val.replace("mailto:", "", 1).split("?", 1)[0].strip()
                user = await self.auth_service.user_repo.get_by_id(user_id)
                from_addr = user.email if user else "me"
                raw_b64 = build_mime_reply(
                    from_addr=from_addr,
                    to_addr=target_addr,
                    subject="Unsubscribe",
                    body="Please unsubscribe me from this mailing list.",
                    is_reply=False,
                )
                await gmail.send_raw_message(raw_b64)
                unsub_method = "mailto"
            except Exception as exc:
                logger.warning("mailto_unsubscribe_failed", error=str(exc))

        # Archive all recent messages from this sender for this user
        archived_count = 0
        archive_permission_missing = False
        all_rows = await self.email_repo.list_newsletters_by_user(user_id=user_id, limit=50)
        target_sender = email.sender_email.lower().strip()
        msg_ids = {
            r["gmail_msg_id"]
            for r in all_rows
            if (r.get("sender_email") or "").lower().strip() == target_sender
        }
        msg_ids.add(email.gmail_msg_id)

        for gm_id in msg_ids:
            try:
                await gmail.archive_message(gm_id)
                archived_count += 1
            except Exception as exc:
                err_str = str(exc)
                if "insufficientPermissions" in err_str or "403" in err_str:
                    archive_permission_missing = True
                    break
                logger.debug("archive_message_skipped", gmail_msg_id=gm_id, error=err_str)

        if (archived_count > 0 or unsub_method != "none") and hasattr(
            self.email_repo, "clear_list_unsubscribe"
        ):
            try:
                await self.email_repo.clear_list_unsubscribe(user_id, list(msg_ids))
            except Exception as exc:
                logger.debug("clear_list_unsubscribe_failed", error=str(exc))

        return {
            "status": "success",
            "sender": email.sender,
            "unsubscribed": unsub_method != "none",
            "method": unsub_method,
            "archived_count": archived_count,
            "archive_permission_missing": archive_permission_missing,
        }
