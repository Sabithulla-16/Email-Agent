"""Voice-first morning briefing synthesis using edge-tts (neural TTS)."""

import asyncio
import io
import re
from typing import Any

import edge_tts
import structlog

logger = structlog.get_logger("app.services.voice")

VOICE_PRESETS: dict[str, tuple[str, str]] = {
    "neerja": ("en-IN-NeerjaNeural", "Indian English (Female)"),
    "prabhat": ("en-IN-PrabhatNeural", "Indian English (Male)"),
    "aria": ("en-US-AriaNeural", "US English (Female)"),
    "guy": ("en-US-GuyNeural", "US English (Male)"),
    "sonia": ("en-GB-SoniaNeural", "British English (Female)"),
}
DEFAULT_VOICE_KEY = "neerja"
DEFAULT_VOICE_NAME = VOICE_PRESETS[DEFAULT_VOICE_KEY][0]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+")


def resolve_voice_name(user_prefs: dict[str, Any] | None) -> str:
    """Resolve per-user TTS voice from user.prefs, defaulting to en-IN-NeerjaNeural."""
    if not user_prefs:
        return DEFAULT_VOICE_NAME
    pref_val = str(user_prefs.get("voice_name") or "").strip()
    if not pref_val:
        return DEFAULT_VOICE_NAME
    lower_val = pref_val.lower()
    if lower_val in VOICE_PRESETS:
        return VOICE_PRESETS[lower_val][0]
    if pref_val.endswith("Neural"):
        return pref_val
    return DEFAULT_VOICE_NAME


def is_voice_enabled(user_prefs: dict[str, Any] | None) -> bool:
    """Return whether voice morning briefing is enabled for this user (default True)."""
    if not user_prefs:
        return True
    return bool(user_prefs.get("voice_briefing", True))


def _clean_for_speech(text: str) -> str:
    no_html = _HTML_TAG_RE.sub("", text)
    no_urls = _URL_RE.sub("", no_html)
    return " ".join(no_urls.split()).strip()


def build_spoken_script(
    user_name: str,
    total_emails: int,
    total_pending: int,
    urgent_emails: list[str],
    email_summaries: list[dict[str, str]],
) -> str:
    """Build a concise, natural-sounding spoken briefing script."""
    name_part = _clean_for_speech(user_name) or "there"
    parts: list[str] = [
        f"Good morning {name_part}. Here is your email briefing.",
        f"You received {total_emails} {'email' if total_emails == 1 else 'emails'} in this window.",
    ]
    if total_pending > 0:
        parts.append(
            f"You also have {total_pending} "
            f"{'draft' if total_pending == 1 else 'drafts'} waiting for your approval."
        )
    if urgent_emails:
        clean_urgent = ", ".join(_clean_for_speech(u) for u in urgent_emails[:2] if u.strip())
        if clean_urgent:
            parts.append(f"High priority items: {clean_urgent}.")

    for item in email_summaries[:4]:
        sender = _clean_for_speech(item.get("sender", "Someone")).split("<")[0].strip()
        summary = _clean_for_speech(item.get("summary", item.get("subject", "")))
        if summary:
            parts.append(f"From {sender}: {summary}.")

    if total_pending > 0:
        parts.append("Check the text digest below, or reply with a voice note to revise a draft.")
    else:
        parts.append("Your full text digest is right below.")

    return " ".join(parts)


class VoiceService:
    """Synthesizes natural neural speech audio bytes using edge-tts."""

    async def synthesize_speech(
        self,
        text: str,
        voice_name: str = DEFAULT_VOICE_NAME,
        timeout_seconds: float = 15.0,
    ) -> bytes:
        """Convert plain text into MP3 audio bytes using edge-tts."""
        clean_text = _clean_for_speech(text)
        if not clean_text:
            return b""

        async def _generate() -> bytes:
            communicate = edge_tts.Communicate(clean_text, voice=voice_name)
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    buf.write(chunk["data"])
            return buf.getvalue()

        return await asyncio.wait_for(_generate(), timeout=timeout_seconds)
