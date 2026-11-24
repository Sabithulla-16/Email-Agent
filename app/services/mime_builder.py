"""RFC 2822 MIME message generator with threading headers and idempotency tags."""

import base64
import re
from email.message import EmailMessage


def build_mime_reply(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    idempotency_key: str | None = None,
    is_reply: bool = True,
) -> str:
    """Build an RFC 2822 MIME message for replying to an existing thread or sending a new email.

    Returns standard base64url string suitable for Gmail API send raw.
    """
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr

    # Subject with Re: handling only for replies
    if is_reply:
        if not re.match(r"(?i)^re:\s*", subject.strip()):
            normalized_subject = f"Re: {subject.strip()}"
        else:
            normalized_subject = subject.strip()
    else:
        normalized_subject = subject.strip()
    msg["Subject"] = normalized_subject

    # Threading RFC headers
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # If references exists, append in_reply_to if not already present
        if references:
            if in_reply_to not in references:
                msg["References"] = f"{references} {in_reply_to}"
            else:
                msg["References"] = references
        else:
            msg["References"] = in_reply_to

    # Custom idempotency tracking header
    if idempotency_key:
        msg["X-Idempotency-Key"] = idempotency_key

    # Email body (UTF-8 plain text)
    msg.set_content(body, charset="utf-8")

    # Encode as base64url
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")
