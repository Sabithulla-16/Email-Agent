"""Thin asynchronous client over the Telegram Bot API using httpx."""

import asyncio
import contextlib
import html
import json
import socket
import ssl
import subprocess
import sys
from typing import Any

import httpx
import structlog

from app.core.config import Settings, get_settings
from app.db.repos.telegram_repo import TelegramRepo
from app.queue.redis import get_redis

# Force IPv4 socket resolution on Windows to prevent dual-stack IPv6 ISP packet drops
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_v4(host: str, port: int | str, _family: int = 0, *args: object) -> list[object]:
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args)  # type: ignore[arg-type]


if sys.platform == "win32":
    socket.getaddrinfo = _getaddrinfo_v4

logger = structlog.get_logger("app.services.telegram_client")

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
MAX_MESSAGE_LENGTH = 4096


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return html.escape(text, quote=False)


def _run_curl_api(
    url: str, payload: dict[str, Any] | None = None, method: str = "POST", timeout: float = 15.0
) -> tuple[int, dict[str, Any]]:
    cmd = ["curl.exe", "-4", "-s", "--max-time", str(int(timeout))]
    if method == "POST":
        cmd.extend(["-X", "POST", "-H", "Content-Type: application/json"])
        if payload is not None:
            cmd.extend(["-d", json.dumps(payload)])
        else:
            cmd.extend(["-d", ""])
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise httpx.RequestError(f"Curl error {res.returncode}: {res.stderr}")
    try:
        data = json.loads(res.stdout)
        status_code = 200 if data.get("ok") else data.get("error_code", 400)
        return status_code, data
    except Exception as exc:
        raise httpx.RequestError(f"Invalid JSON from Telegram: {res.stdout[:100]}") from exc


class TelegramClient:
    """Low-overhead async client for Telegram Bot API with rate limiting and 403 handling."""

    def __init__(
        self,
        bot_token: str | None = None,
        telegram_repo: TelegramRepo | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.bot_token = bot_token or self.settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        self.base_url = f"{TELEGRAM_API_BASE}{self.bot_token}"
        self.telegram_repo = telegram_repo
        self._ssl_ctx = ssl.create_default_context()
        with contextlib.suppress(AttributeError):
            self._ssl_ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT

    async def _enforce_rate_limit(self, chat_id: int) -> None:
        """Rate limit outgoing calls (~1 msg/sec per chat, global limit ~30 msg/sec).

        Uses Redis token bucket when Redis is reachable, falls back smoothly.
        """
        try:
            redis = get_redis()
        except Exception:
            redis = None

        if not redis:
            await asyncio.sleep(0.05)
            return

        chat_key = f"rate:tg:chat:{chat_id}"
        global_key = "rate:tg:global"

        try:
            # Per-chat rate limit (1 msg / sec)
            chat_count = await redis.incr(chat_key)
            if chat_count == 1:
                await redis.expire(chat_key, 1)
            elif chat_count > 1:
                await asyncio.sleep(1.0)

            # Global rate limit (~30 msg / sec)
            global_count = await redis.incr(global_key)
            if global_count == 1:
                await redis.expire(global_key, 1)
            elif global_count > 30:
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug("telegram_rate_limiter_redis_bypass", error=str(exc))
            await asyncio.sleep(0.05)

    async def _post_request(
        self, endpoint: str, payload: dict[str, Any], chat_id: int | None = None
    ) -> dict[str, Any] | None:
        """Execute POST request with exponential retry on 429 and handling for 403."""
        if chat_id:
            await self._enforce_rate_limit(chat_id)

        url = f"{self.base_url}/{endpoint}"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                try:
                    async with httpx.AsyncClient(
                        verify=self._ssl_ctx,
                        timeout=10.0,
                        headers={"Connection": "close"},
                    ) as client:
                        resp = await client.post(url, json=payload)
                        status_code = resp.status_code
                        data = resp.json()
                except httpx.RequestError:
                    if sys.platform == "win32":
                        status_code, data = await asyncio.to_thread(
                            _run_curl_api, url, payload, "POST", 15.0
                        )
                    else:
                        raise

                # Handle 403: Bot blocked by the user
                if status_code == 403:
                    logger.warning(
                        "telegram_bot_blocked_by_user",
                        chat_id=chat_id,
                        endpoint=endpoint,
                    )
                    if chat_id and self.telegram_repo:
                        await self.telegram_repo.deactivate_chat(chat_id)
                    return None

                # Handle 429: Too Many Requests with retry-after header or JSON param
                if status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after") or 5
                    logger.warning(
                        "telegram_rate_limit_429",
                        retry_after=retry_after,
                        attempt=attempt,
                    )
                    await asyncio.sleep(float(retry_after))
                    continue

                if not data.get("ok"):
                    logger.error("telegram_api_error", data=data)
                    return None

                return data.get("result")

            except httpx.HTTPStatusError as exc:
                logger.error(
                    "telegram_api_http_error",
                    status=exc.response.status_code,
                    detail=exc.response.text,
                    attempt=attempt,
                )
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1.0 * (2**attempt))
            except httpx.RequestError as exc:
                logger.error("telegram_network_error", error=str(exc), attempt=attempt)
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1.0 * (2**attempt))

        return None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | None:
        """Send message to Telegram chat. Truncates gracefully if text exceeds 4096 chars."""
        final_text = text
        if len(final_text) > MAX_MESSAGE_LENGTH:
            suffix = (
                f"\n\n<i>[Message truncated — view in web: {self.settings.FRONTEND_BASE_URL}]</i>"
            )
            final_text = final_text[: MAX_MESSAGE_LENGTH - len(suffix)] + suffix

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": final_text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        return await self._post_request("sendMessage", payload, chat_id=chat_id)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | None:
        """Edit message text in Telegram chat."""
        final_text = text
        if len(final_text) > MAX_MESSAGE_LENGTH:
            suffix = (
                f"\n\n<i>[Message truncated — view in web: {self.settings.FRONTEND_BASE_URL}]</i>"
            )
            final_text = final_text[: MAX_MESSAGE_LENGTH - len(suffix)] + suffix

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": final_text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        return await self._post_request("editMessageText", payload, chat_id=chat_id)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Delete a message from a chat."""
        try:
            payload = {"chat_id": chat_id, "message_id": message_id}
            res = await self._post_request("deleteMessage", payload, chat_id=chat_id)
            return bool(res)
        except Exception as exc:
            logger.debug(
                "telegram_delete_message_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(exc),
            )
            return False

    async def get_me(self) -> dict[str, Any] | None:
        """Fetch bot metadata (id, username, first_name)."""
        return await self._post_request("getMe", {})

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        """Get metadata for a Telegram file."""
        return await self._post_request("getFile", {"file_id": file_id})

    async def download_file(self, file_path: str) -> bytes:
        """Download raw file bytes from Telegram file storage."""
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        async with httpx.AsyncClient(
            verify=self._ssl_ctx,
            timeout=30.0,
            headers={"Connection": "close"},
        ) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            return resp.content

    async def set_webhook(self, url: str, secret_token: str) -> bool:
        """Configure webhook endpoint for Telegram Bot API."""
        payload = {
            "url": url,
            "secret_token": secret_token,
            "allowed_updates": ["message", "callback_query"],
        }
        res = await self._post_request("setWebhook", payload)
        return bool(res)

    async def get_webhook_info(self) -> dict[str, Any] | None:
        """Fetch current webhook configuration and pending update count."""
        res = await self._post_request("getWebhookInfo", {})
        if res and isinstance(res, dict) and "result" in res:
            return res["result"]
        return res

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        """Delete webhook endpoint (only used when running in polling mode)."""
        res = await self._post_request(
            "deleteWebhook", {"drop_pending_updates": drop_pending_updates}
        )
        return bool(res)
