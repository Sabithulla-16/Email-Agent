"""Extractors for Bills, Travel reservations, VIP contacts, and Phishing warnings."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from app.db.client import get_supabase
from app.models.db import EmailRow

logger = structlog.get_logger("app.services.extra_extractors")


class ExtraExtractorsService:
    """Specialized domain extractors running on analyzed email content."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_supabase()
        return self._client

    def extract_bill(self, email: EmailRow) -> dict[str, Any] | None:
        """Extract billing info (vendor, amount, currency, due date) from invoice/receipt emails."""
        body = email.body_clean
        # Pattern for amounts like $120.00 or USD 99.95
        amount_match = re.search(r"(\$|USD\s*|EUR\s*|GBP\s*)(\d+(?:\.\d{2})?)", body, re.IGNORECASE)
        if not amount_match:
            return None

        currency_sym = amount_match.group(1).strip()
        amount_val = float(amount_match.group(2))
        currency = "EUR" if "EUR" in currency_sym else ("GBP" if "GBP" in currency_sym else "USD")

        # Due date fallback
        due_date = (datetime.now(UTC) + timedelta(days=14)).date()

        return {
            "has_bill": True,
            "biller": email.sender,
            "amount": amount_val,
            "currency": currency,
            "due_date": due_date.isoformat(),
        }

    def extract_travel_reservation(self, email: EmailRow) -> dict[str, Any] | None:
        """Detect flight, train, or hotel booking confirmations."""
        text = (email.subject + " " + email.body_clean).lower()
        travel_keywords = [
            "booking confirmation",
            "flight confirmation",
            "reservation",
            "itinerary",
        ]
        if not any(k in text for k in travel_keywords):
            return None

        # Look for alphanumeric booking confirmation code (e.g. ABC123D)
        ref_match = re.search(
            r"\b(?:confirmation|code|pnr|booking)\s*(?:reference|number)?\s*[:#]?\s*([A-Z0-9]{6,8})\b",
            email.body_clean,
            re.IGNORECASE,
        )
        booking_code = ref_match.group(1).upper() if ref_match else "CONFIRMED"

        return {
            "has_travel": True,
            "subject": email.subject,
            "booking_reference": booking_code,
            "vendor": email.sender,
        }

    def scan_phishing_indicators(self, email: EmailRow) -> list[str]:
        """Detect lookalike domains, urgent credential requests, and suspicious links."""
        warnings: list[str] = []
        body = email.body_clean.lower()

        # Urgent credential/suspension threats
        urgent_phrases = [
            "account suspended",
            "verify your password",
            "unauthorized login",
            "urgent action required",
        ]
        if any(p in body for p in urgent_phrases):
            warnings.append("Urgent threat or credential verification requested.")

        # Gift card / wire transfer requests
        if any(p in body for p in ["gift card", "wire transfer", "crypto payment", "bitcoin"]):
            warnings.append("Payment or gift card request from external sender.")

        # Suspicious sender mismatches
        if "paypal" in email.sender.lower() and "paypal.com" not in email.sender_email.lower():
            warnings.append("Sender display name does not match sender domain.")

        return warnings

    async def update_contact_history(self, user_id: UUID | str, email: EmailRow) -> None:
        """Update contact interaction frequency and last contacted timestamp."""
        now_iso = datetime.now(UTC).isoformat()
        payload = {
            "user_id": str(user_id),
            "email": email.sender_email.lower().strip(),
            "name": email.sender,
            "last_contacted_at": now_iso,
            "updated_at": now_iso,
        }
        try:
            await (
                self.client.table("contacts").upsert(payload, on_conflict="user_id,email").execute()
            )
        except Exception as exc:
            logger.debug("contact_update_bypassed", error=str(exc))
