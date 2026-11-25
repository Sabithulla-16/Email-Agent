"""Inbox Q&A engine powered by Groq LLMs over daily digested email archives."""

import re
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.email_repo import EmailRepo
from app.services.groq_service import GroqService

logger = structlog.get_logger("app.services.inbox_qa")


class InboxQAService:
    """Answers user natural language questions grounded in their digested email archive."""

    def __init__(
        self,
        email_repo: EmailRepo,
        settings: Settings | None = None,
        groq_service: GroqService | None = None,
        auth_service: Any | None = None,
    ) -> None:
        self.email_repo = email_repo
        self.settings = settings or get_settings()
        self.groq_service = groq_service or GroqService(settings=self.settings)
        self.auth_service = auth_service
        # Expose groq_client for backward compatibility with mock tests
        self.groq_client = self.groq_service.client

    async def answer_question(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 50,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Answer natural language question based on the user's digested email catalog."""
        user_key = api_key
        if (
            not user_key
            and self.auth_service
            and hasattr(self.auth_service, "get_user_groq_api_key")
        ):
            try:
                res_k = self.auth_service.get_user_groq_api_key(user_id)
                if hasattr(res_k, "__await__"):
                    user_key = await res_k
                else:
                    user_key = res_k
            except Exception:
                user_key = None

        # 1. Fetch recent synced emails for the user
        emails = await self.email_repo.list_by_user(user_id=user_id, limit=limit)
        if not emails:
            return {
                "answer": "I checked your inbox and found no emails in your account.",
                "citations": [],
            }

        # 2. Build digested email catalog (Sender, Subject, Date, Summary)
        catalog_entries: list[str] = []
        indexed_emails: dict[int, Any] = {}

        for idx, em in enumerate(emails, start=1):
            indexed_emails[idx] = em
            # Retrieve cached analysis summary or generate fallback digest
            summary_text = ""
            try:
                analysis = await self.email_repo.get_analysis(em.id)
                if analysis and analysis.summary:
                    summary_text = analysis.summary
            except Exception:
                pass

            if not summary_text:
                summary_text = (em.snippet or em.subject or "No content available.")[:250]

            date_str = em.received_at.strftime("%Y-%m-%d %H:%M") if em.received_at else "Recent"
            catalog_entries.append(
                f"[Email #{idx}]\n"
                f"Date: {date_str}\n"
                f"From: {em.sender} <{em.sender_email}>\n"
                f"Subject: {em.subject}\n"
                f"Digest: {summary_text.strip()}\n"
            )

        catalog_text = "\n---\n".join(catalog_entries)

        # 3. Formulate grounded prompt for Groq LLM
        prompt = (
            "You are an intelligent, factual AI email assistant answering user questions "
            "based strictly on their digested emails below.\n\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. Base your answer ONLY on the facts present in the digested email catalog.\n"
            "2. If the user asks whether they received any email regarding a specific sender, "
            "product, company, or topic (e.g. Spotify, flights, invoices, passwords):\n"
            "   - If matching emails exist in the catalog, answer clearly using the digest details "
            "and cite the source using [Email #X].\n"
            "   - If NO email in the catalog is about that topic or sender, state clearly: "
            "'I checked your emails and you haven't received any emails regarding [topic].' "
            "Do NOT cite or mention unrelated emails.\n"
            "3. NEVER fabricate an email citation or mention an [Email #X] tag that does "
            "not exist.\n"
            "4. Only cite [Email #X] for emails that directly answer the inquiry.\n\n"
            "<digested_email_catalog>\n"
            f"{catalog_text}\n"
            "</digested_email_catalog>\n\n"
            f"User Question: {query}"
        )

        # 4. Invoke LLM with automatic model fallback (gpt-oss-120b -> qwen3.8-27b -> gpt-oss-20b)
        try:
            answer_text, _ = await self.groq_service.chat_completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                api_key=user_key,
            )
        except Exception as exc:
            logger.warning("inbox_qa_fallback_error", error=str(exc))
            try:
                resp = await self.groq_client.chat.completions.create(
                    model=self.settings.GROQ_FAST_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                )
                answer_text = (resp.choices[0].message.content or "").strip()
            except Exception as direct_exc:
                logger.error("inbox_qa_all_models_failed", error=str(direct_exc))
                return {
                    "answer": f"Unable to process question right now: {direct_exc}",
                    "citations": [],
                }

        # 5. Extract only valid citations explicitly referenced in the LLM's answer
        cited_indices = set()
        matches = re.findall(r"Email\s*#(\d+)", answer_text, re.IGNORECASE)
        for m in matches:
            try:
                num = int(m)
                if num in indexed_emails:
                    cited_indices.add(num)
            except ValueError:
                continue

        citations: list[dict[str, Any]] = []
        for idx in sorted(cited_indices):
            em = indexed_emails[idx]
            citations.append(
                {
                    "ref": f"Email #{idx}",
                    "id": str(em.id),
                    "sender": em.sender,
                    "subject": em.subject,
                    "date": em.received_at.isoformat() if em.received_at else "",
                }
            )

        return {
            "answer": answer_text,
            "citations": citations,
        }

    async def search_emails(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Encrypted-safe semantic/keyword search over a single user's email archive."""
        if hasattr(self.email_repo, "search_decrypted"):
            return await self.email_repo.search_decrypted(user_id=user_id, query=query, limit=limit)
        return []
