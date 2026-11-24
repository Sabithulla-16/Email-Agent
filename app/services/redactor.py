"""PII and Sensitive Credential Redactor & Security/OTP Pre-filter."""

import re

# Credit Card regex (standard 13-19 digits formatted or unformatted)
CARD_PATTERN = re.compile(
    r"\b(?:\d{4}[ -]?){3}\d{4}\b|\b(?:\d{4}[ -]?){2}\d{5}\b",
    re.ASCII,
)

# OTP / Verification Code regex (4 to 8 digits near verification keywords)
OTP_CONTEXT_PATTERN = re.compile(
    r"(?i)\b(?:verification|verification\s+code|security\s+code|"
    r"one[- ]time\s+password|otp|passcode|pin|access\s+code)\b(?:\s+is)?[:\s\-—]+([0-9]{4,8})\b"
)

# Generic password assignment regex
PASSWORD_PATTERN = re.compile(
    r"(?i)\b(?:password|temporary\s+password|initial\s+password)\s*[:=]\s*([^\s]{6,32})\b"
)

# Security / OTP email classification patterns (to skip LLM entirely)
SECURITY_EMAIL_PATTERNS = [
    re.compile(
        r"(?i)(one[- ]time\s+password|verification\s+code|your\s+login\s+code|"
        r"two[- ]factor\s+authentication|security\s+alert|new\s+sign[- ]in)"
    ),
    re.compile(r"(?i)(reset\s+your\s+password|confirm\s+your\s+identity|access\s+code\s+is)"),
]


def redact_sensitive_data(text: str) -> str:
    """Redact payment card numbers, OTPs, and plaintext passwords from email bodies."""
    redacted = text

    # 1. Redact credit card numbers
    redacted = CARD_PATTERN.sub("[REDACTED_PAYMENT_CARD]", redacted)

    # 2. Redact OTPs
    def _mask_otp(match: re.Match[str]) -> str:
        full_match = match.group(0)
        code = match.group(1)
        return full_match.replace(code, "[REDACTED_OTP]")

    redacted = OTP_CONTEXT_PATTERN.sub(_mask_otp, redacted)

    # 3. Redact passwords
    def _mask_password(match: re.Match[str]) -> str:
        full_match = match.group(0)
        pwd = match.group(1)
        return full_match.replace(pwd, "[REDACTED_PASSWORD]")

    redacted = PASSWORD_PATTERN.sub(_mask_password, redacted)

    return redacted


def is_security_or_otp_email(subject: str, snippet: str, body: str) -> bool:
    """Check if the email matches automated security/OTP alerts to skip LLM processing."""
    combined = f"{subject} {snippet} {body[:500]}".lower()
    return any(pattern.search(combined) for pattern in SECURITY_EMAIL_PATTERNS)
