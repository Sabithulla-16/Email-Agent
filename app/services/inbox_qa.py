"""Inbox Q&A engine powered by Groq LLMs over daily digested email archives."""

import re
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repos.email_repo import EmailRepo
from app.services.groq_service import GroqService

logger = structlog.get_logger("app.services.inbox_qa")

TELEGRAM_COMMANDS_CONTEXT = """Available Telegram Bot Commands:
- /create <to> <notes> : Compose a fresh email draft with AI for your review.
- /schedule : Guided 3-step email scheduler with preview, revise, and accept.
- /manage (or /scheduled) : Edit content/recipient, reschedule, or cancel scheduled emails.
- /pending : Review and approve/revise/reject waiting AI reply drafts.
- /cancel : Cancel an active /create or /schedule flow.
- /ask <question> : Ask a factual question strictly over your email archive.
- /find <keyword> : Search your encrypted email archive by keyword/topic.
- /radar : Decision & Commitment Radar (who is waiting on you & open follow-ups).
- /followups : View tracked promises & commitments from your emails.
- /urgent : Show high-priority unread emails.
- /unsubscribe : One-tap newsletter & promo purge (unsubscribe and archive).
- /sync : Manually pull and analyze new emails from Gmail right now.
- /digest : Compile and send your email digest (text + voice briefing) now.
- /voice : Send text + voice briefing (/voice list, /voice <name>, /voice on|off).
- /signature <text> : Set your email sign-off signature (or /signature clear).
- /setkey <gsk_...> : Set your personal Groq API key.
- /mykey / /removekey : Check or remove your personal Groq API key.
- /snooze <hours> / /mute : Pause notifications for N hours or 24h.
- /settings : View current notification settings.
- /reconnect : Re-authorize Gmail permissions.
- /disconnect : Unlink this Telegram chat.
- /help : Show the full command reference."""


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

    async def answer_free_text(
        self,
        user_id: UUID | str,
        query: str,
        limit: int = 50,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Handle free-form Telegram text: answer from emails OR suggest the right bot command."""
        user_key = api_key
        if (
            not user_key
            and self.auth_service
            and hasattr(self.auth_service, "get_user_groq_api_key")
        ):
            try:
                res_k = self.auth_service.get_user_groq_api_key(user_id)
                user_key = await res_k if hasattr(res_k, "__await__") else res_k
            except Exception:
                user_key = None

        emails = await self.email_repo.list_by_user(user_id=user_id, limit=limit)
        catalog_entries: list[str] = []
        indexed_emails: dict[int, Any] = {}

        for idx, em in enumerate(emails, start=1):
            indexed_emails[idx] = em
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

        catalog_text = (
            "\n---\n".join(catalog_entries) if catalog_entries else "No synced emails available."
        )

        prompt = (
            "You are a helpful AI email assistant inside Telegram.\n"
            "You have two sources of context:\n"
            "1) <available_bot_commands>: the exact slash commands this bot supports.\n"
            "2) <digested_email_catalog>: the user's recent digested emails.\n\n"
            "INSTRUCTIONS:\n"
            "1. If the user asks about their emails and relevant facts exist in "
            "<digested_email_catalog>, answer concisely using ONLY those facts and cite "
            "[Email #X].\n"
            "2. If the user wants to perform an action (schedule, compose, change signature, "
            "change voice, unsubscribe, sync, view drafts, radar) OR if NO email in "
            "<digested_email_catalog> answers their question, clearly suggest the most "
            "suitable command(s) from <available_bot_commands> with a short usage example.\n"
            "3. NEVER fabricate email facts or cite an [Email #X] that does not exist.\n\n"
            "<available_bot_commands>\n"
            f"{TELEGRAM_COMMANDS_CONTEXT}\n"
            "</available_bot_commands>\n\n"
            "<digested_email_catalog>\n"
            f"{catalog_text}\n"
            "</digested_email_catalog>\n\n"
            f"User Message: {query}"
        )

        try:
            answer_text, _ = await self.groq_service.chat_completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                api_key=user_key,
            )
        except Exception as exc:
            logger.warning("free_text_assistant_failed", error=str(exc))
            return {
                "answer": (
                    "I couldn't process that right now. "
                    "Use /help to view all commands, /ask <question> for email Q&A, "
                    "or /find <keyword> to search."
                ),
                "citations": [],
            }

        cited_indices = set()
        for m in re.findall(r"Email\s*#(\d+)", answer_text, re.IGNORECASE):
            try:
                num = int(m)
                if num in indexed_emails:
                    cited_indices.add(num)
            except ValueError:
                continue

        citations = [
            {
                "ref": f"Email #{idx}",
                "id": str(indexed_emails[idx].id),
                "sender": indexed_emails[idx].sender,
                "subject": indexed_emails[idx].subject,
                "date": (
                    indexed_emails[idx].received_at.isoformat()
                    if indexed_emails[idx].received_at
                    else ""
                ),
            }
            for idx in sorted(cited_indices)
        ]
        return {"answer": answer_text, "citations": citations}

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
