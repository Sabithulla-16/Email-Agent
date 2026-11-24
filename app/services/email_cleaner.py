"""Email MIME parsing, HTML to clean text conversion, and reply/signature stripping."""

import base64
import re
from typing import Any

from bs4 import BeautifulSoup

# Regex patterns for quoted replies
QUOTE_PATTERNS = [
    re.compile(r"^On\s+.+?wrote:.*$", re.MULTILINE | re.DOTALL),
    re.compile(r"^-----Original Message-----.*$", re.MULTILINE | re.DOTALL),
    re.compile(r"^_{10,}.*$", re.MULTILINE | re.DOTALL),
    re.compile(r"^From:\s+.+?\nSent:\s+.+?\nTo:\s+.+?\nSubject:.*$", re.MULTILINE | re.DOTALL),
]

# Regex patterns for email signatures
SIGNATURE_PATTERNS = [
    re.compile(r"\n--\s*\n.*$", re.DOTALL),
    re.compile(
        r"\nSent from my (iPhone|Android|Galaxy|mobile device).*$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\nGet Outlook for (iOS|Android).*$", re.IGNORECASE | re.DOTALL),
]

BULK_SENDER_PATTERNS = re.compile(
    r"(no[-_]?reply|donotreply|notifications|mailer-daemon|bounces|newsletter|marketing)@",
    re.IGNORECASE,
)


def extract_header_value(headers: list[dict[str, str]], name: str) -> str | None:
    """Case-insensitive header value lookup."""
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value")
    return None


def extract_mime_body(payload: dict[str, Any]) -> tuple[str, bool]:
    """Walk MIME parts and extract plain text or converted HTML body.

    Returns (raw_body, is_html). Prefers text/plain.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    # Single part text/plain
    if mime_type == "text/plain" and body_data:
        return _decode_b64url(body_data), False

    # Single part text/html
    if mime_type == "text/html" and body_data:
        return _decode_b64url(body_data), True

    # Multipart - traverse parts preferring text/plain
    plain_candidate: str | None = None
    html_candidate: str | None = None

    parts = payload.get("parts", [])
    for part in parts:
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data")

        if part_mime == "text/plain" and part_data:
            plain_candidate = _decode_b64url(part_data)
        elif part_mime == "text/html" and part_data:
            html_candidate = _decode_b64url(part_data)
        elif part.get("parts"):
            # Nested multipart (e.g. multipart/related inside multipart/alternative)
            nested_body, nested_is_html = extract_mime_body(part)
            if nested_body:
                if nested_is_html:
                    html_candidate = html_candidate or nested_body
                else:
                    plain_candidate = plain_candidate or nested_body

    if plain_candidate:
        return plain_candidate, False
    if html_candidate:
        return html_candidate, True

    return "", False


def _decode_b64url(data: str) -> str:
    """Decode base64url encoded string from Gmail API."""
    padded = data.replace("-", "+").replace("_", "/")
    padding = len(padded) % 4
    if padding:
        padded += "=" * (4 - padding)
    try:
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def clean_html_to_text(html_content: str) -> str:
    """Convert HTML email body into clean, normalized plain text."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove script, style, and hidden tracking pixels
    for element in soup(["script", "style", "head", "title", "meta", "noscript"]):
        element.extract()

    # Extract text with line breaks preserved
    text = soup.get_text(separator="\n")
    # Collapse multiple blank lines
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def strip_quotes_and_signatures(text: str) -> str:
    """Strip quoted previous email threads and typical sign-offs."""
    cleaned = text

    # Remove quoted replies
    for pattern in QUOTE_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    # Remove standard signatures
    for pattern in SIGNATURE_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    return cleaned.strip()


def sanitize_email_body(raw_body: str, is_html: bool, max_chars: int = 15000) -> str:
    """Full preprocessing pipeline: HTML conversion, quote/signature removal, and length cap."""
    text = clean_html_to_text(raw_body) if is_html else raw_body
    cleaned = strip_quotes_and_signatures(text)

    # Length capping
    if len(cleaned) > max_chars:
        trunc_msg = f"\n\n[... Email body truncated at {max_chars} characters ...]"
        cleaned = cleaned[:max_chars] + trunc_msg

    return cleaned


def extract_attachments_metadata(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract attachment metadata only (filename, mimeType, size) without downloading bytes."""
    attachments: list[dict[str, Any]] = []

    def _walk(parts: list[dict[str, Any]]) -> None:
        for p in parts:
            filename = p.get("filename")
            body = p.get("body", {})
            attachment_id = body.get("attachmentId")
            if filename and attachment_id:
                attachments.append(
                    {
                        "filename": filename,
                        "mime_type": p.get("mimeType", "application/octet-stream"),
                        "size_bytes": body.get("size", 0),
                        "attachment_id": attachment_id,
                    }
                )
            if p.get("parts"):
                _walk(p["parts"])

    if payload.get("parts"):
        _walk(payload["parts"])

    return attachments


def is_bulk_or_noreply(
    sender: str,
    list_unsubscribe: str | None = None,
    precedence: str | None = None,
) -> bool:
    """Determine whether the email is automated, newsletter, or bulk notification."""
    if list_unsubscribe:
        return True
    if precedence and precedence.lower() in {"bulk", "list", "junk"}:
        return True
    return bool(BULK_SENDER_PATTERNS.search(sender))
