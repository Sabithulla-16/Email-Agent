"""Inbox Q&A engine with grounded citations over email archives using Groq LLMs."""

from typing import Any
from uuid import UUID

import structlog
from groq import AsyncGroq

from app.core.config import Settings, get_settings
from app.db.repos.email_repo import EmailRepo

logger = structlog.get_logger("app.services.inbox_qa")


class InboxQAService:
    """Answers user natural language questions grounded exclusively in their email archive."""

    def __init__(
        self,
        email_repo: EmailRepo,
        settings: Settings | None = None,
    ) -> None:
        self.email_repo = email_repo
        self.settings = settings or get_settings()
        self.groq_client = AsyncGroq(api_key=self.settings.GROQ_API_KEY.get_secret_value())

    async def answer_question(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Retrieve relevant email records and generate a grounded, cited answer."""
        # 1. Retrieve candidate emails
        emails = await self.email_repo.list_by_user(user_id=user_id, limit=20)
        query_lower = query.lower()

        # Score relevance simply if semantic vector not yet populated
        ranked = []
        for e in emails:
            score = 0
            if any(w in e.subject.lower() for w in query_lower.split()):
                score += 2
            ranked.append((score, e))

        ranked.sort(key=lambda x: x[0], reverse=True)
        top_items = [item[1] for item in ranked[:limit]]

        if not top_items:
            return {
                "answer": "No relevant emails were found in your inbox regarding that topic.",
                "citations": [],
            }

        # 2. Build grounded context with full email bodies
        context_parts: list[str] = []
        citations: list[dict[str, Any]] = []

        for idx, item in enumerate(top_items, 1):
            full_email = await self.email_repo.get_by_id(item.id)
            if not full_email:
                continue

            cite_ref = f"Email #{idx}"
            citations.append(
                {
                    "ref": cite_ref,
                    "id": str(full_email.id),
                    "sender": full_email.sender,
                    "subject": full_email.subject,
                    "date": full_email.received_at.isoformat(),
                }
            )
            context_parts.append(
                f"[{cite_ref}]\n"
                f"Date: {full_email.received_at}\n"
                f"From: {full_email.sender}\n"
                f"Subject: {full_email.subject}\n"
                f"Content:\n{full_email.body_clean[:1000]}\n"
            )

        grounded_context = "\n---\n".join(context_parts)

        # 3. Call Groq LLM with strict grounding instructions
        prompt = (
            "You are an AI email assistant answering questions based solely on the user's emails.\n"
            "Rules:\n"
            "1. Answer the question using ONLY the provided email context below.\n"
            "2. Whenever stating a fact, cite the email using bracketed notation like [Email #1].\n"
            "3. If the emails do not contain enough information to answer, state that clearly.\n\n"
            f"<email_archive_context>\n{grounded_context}\n</email_archive_context>\n\n"
            f"User Question: {query}"
        )

        resp = await self.groq_client.chat.completions.create(
            model=self.settings.GROQ_FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        answer_text = (resp.choices[0].message.content or "").strip()

        return {
            "answer": answer_text,
            "citations": citations,
        }
