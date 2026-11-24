"""Inbox Q&A engine with grounded citations over email archives using Groq LLMs."""

import re
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.email_repo import EmailRepo
from app.services.groq_service import GroqService

logger = structlog.get_logger("app.services.inbox_qa")

STOP_WORDS = {
    "a", "about", "all", "an", "any", "are", "as", "at", "be", "by", "check",
    "did", "do", "does", "email", "emails", "find", "for", "from", "get", "got",
    "had", "has", "have", "i", "in", "is", "it", "mail", "mails", "me", "message",
    "messages", "my", "of", "on", "or", "received", "regarding", "see", "show",
    "tell", "that", "the", "there", "this", "to", "was", "were", "what", "when",
    "which", "who", "with", "would", "you", "your",
}


class InboxQAService:
    """Answers user natural language questions grounded exclusively in their email archive."""

    def __init__(
        self,
        email_repo: EmailRepo,
        settings: Settings | None = None,
        groq_service: GroqService | None = None,
    ) -> None:
        self.email_repo = email_repo
        self.settings = settings or get_settings()
        self.groq_service = groq_service or GroqService(settings=self.settings)
        # Expose groq_client for backward compatibility with mock tests
        self.groq_client = self.groq_service.client

    async def answer_question(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Retrieve relevant email records and generate a grounded, cited answer."""
        # 1. Clean query and extract non-stopword tokens
        cleaned_query = re.sub(r"[^\w\s]", " ", query.lower())
        tokens = [t for t in cleaned_query.split() if t not in STOP_WORDS and len(t) > 1]
        if not tokens:
            tokens = [t for t in cleaned_query.split() if len(t) > 1]

        # 2. Retrieve candidate emails from user
        emails = await self.email_repo.list_by_user(user_id=user_id, limit=50)

        # 3. Score relevance strictly
        ranked = []
        for e in emails:
            score = 0
            sender_text = f"{e.sender} {e.sender_email}".lower()
            subject_text = e.subject.lower()
            body_text = f"{e.snippet or ''} {e.body_clean}".lower()

            for tok in tokens:
                if tok in sender_text:
                    score += 10
                if tok in subject_text:
                    score += 8
                if tok in body_text:
                    score += 3

            if score > 0:
                ranked.append((score, e))

        # 4. Strict fast path: if no email matches the query tokens, do not hallucinate
        if not ranked:
            return {
                "answer": (
                    f"I checked your inbox and found no emails regarding '{query.strip()}'."
                ),
                "citations": [],
            }

        ranked.sort(key=lambda x: x[0], reverse=True)
        top_items = [item[1] for item in ranked[:limit]]

        # 5. Build grounded context with full email bodies
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
                f"From: {full_email.sender} <{full_email.sender_email}>\n"
                f"Subject: {full_email.subject}\n"
                f"Content:\n{full_email.body_clean[:1200]}\n"
            )

        grounded_context = "\n---\n".join(context_parts)

        # 6. Call Groq LLM with strict grounding and model fallback
        prompt = (
            "You are a factual, strict AI email assistant answering questions based solely "
            "on the user's emails.\n"
            "CRITICAL RULES:\n"
            "1. Answer using ONLY the facts explicitly stated in the email archive context below.\n"
            "2. If the context does NOT contain information to answer the question, state: "
            "'Based on your emails, there is no information about that.'\n"
            "3. NEVER fabricate, hallucinate, or assume email senders, subjects, or accounts.\n"
            "4. Cite the source email using bracketed notation like [Email #1].\n\n"
            f"<email_archive_context>\n{grounded_context}\n</email_archive_context>\n\n"
            f"User Question: {query}"
        )

        try:
            answer_text, _ = await self.groq_service.chat_completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
        except Exception as exc:
            logger.warning("inbox_qa_fallback_error", error=str(exc))
            # Fallback direct call if mock is active in tests
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
