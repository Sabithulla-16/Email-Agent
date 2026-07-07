from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
import json
from src.tools.calendar_api import get_availability
from datetime import datetime, timedelta
from src.db.helpers import get_user_name
from src.tools.github_api import enrich_email_with_github_data # 🔥 NEW IMPORT

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

# In-memory store for pending quick replies
pending_quick_replies: dict = {}

def check_quick_reply(email_text: str, email_id: str, user_uuid: str, sender: str, subject: str, thread_id: str) -> dict | None:
    """Determines if an email is a quick-reply candidate."""
    prompt = ChatPromptTemplate.from_template(
        """You are an email triage assistant. Determine if this email can be answered with a quick, short reply.
        Good candidates: Simple yes/no questions, meeting confirmations, availability checks, status updates.
        Bad candidates: Complex requests, newsletters, spam, emails requiring detailed responses.
        If it IS a quick-reply candidate, return a JSON object:
        {{
            "is_quick_reply": true,
            "options": [
                {{"label": "Button Text", "intent": "What to say in the reply"}},
                {{"label": "Button Text", "intent": "What to say in the reply"}},
                {{"label": "Button Text", "intent": "What to say in the reply"}}
            ]
        }}
        If it is NOT a quick-reply candidate, return:
        {{
            "is_quick_reply": false,
            "options": []
        }}
        Maximum 3 options. Keep labels short (under 30 characters).
        Return ONLY valid JSON. No markdown.
        Email: {email_text}"""
    )
    try:
        chain = prompt | groq_llm
        result = chain.invoke({"email_text": email_text[:2000]})
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        data = json.loads(clean_text)
        if data.get('is_quick_reply') and data.get('options'):
            reply_id = f"qr_{email_id}"
            pending_quick_replies[reply_id] = {
                'user_uuid': user_uuid,
                'sender': sender,
                'subject': subject,
                'thread_id': thread_id,
                'message_id': email_id,
                'snippet': email_text[:500], # 🔥 NEW: Store snippet for GitHub scanning
                'options': data['options']
            }
            logger.info(f"⚡ Quick reply candidate detected: {subject}")
            return {'reply_id': reply_id, 'options': data['options']}
        return None
    except Exception as e:
        logger.error(f"Quick reply check failed: {e}")
        return None

#  CHANGED TO ASYNC DEF
async def generate_quick_reply_email(sender: str, subject: str, intent: str, original_email_snippet: str, user_uuid: str = None) -> dict | None:
    """Generates a short, professional reply email with GitHub and Calendar context."""
    
    # 🔥 ENHANCEMENT: Check if this is a scheduling-related intent
    scheduling_keywords = ['time', 'schedule', 'meeting', 'available', 'calendar', 'slot', 'propose']
    is_scheduling = any(keyword in intent.lower() for keyword in scheduling_keywords)
    
    user_name = get_user_name(user_uuid) if user_uuid else ""
    
    # 1. Fetch Calendar Context (if scheduling)
    calendar_context = ""
    if is_scheduling and user_uuid:
        try:
            from src.tools.google_auth import get_valid_credentials
            creds = get_valid_credentials(user_uuid)
            if creds:
                tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
                availability = get_availability(creds, tomorrow, days=3)
                if availability:
                    calendar_context = "\nUSER'S CALENDAR AVAILABILITY (next 3 days):\n"
                    for date, info in availability.items():
                        if info['free_slots']:
                            calendar_context += f"- {date}: {', '.join(info['free_slots'])}\n"
        except Exception as e:
            logger.warning(f"Could not fetch calendar availability: {e}")

    # 2. 🔥 NEW: Fetch GitHub Context
    github_context = ""
    if user_uuid and original_email_snippet:
        try:
            # Call the GitHub enrichment tool
            enriched_text = await enrich_email_with_github_data(original_email_snippet, user_uuid)
            # If the text changed, it means GitHub links were found and enriched
            if enriched_text != original_email_snippet:
                # Extract just the GitHub context part
                if "--- GITHUB CONTEXT (Live) ---" in enriched_text:
                    github_data = enriched_text.split("--- GITHUB CONTEXT (Live) ---\n")[-1]
                    github_context = f"\n\nLIVE GITHUB DATA DETECTED IN EMAIL:\n{github_data}"
        except Exception as e:
            logger.warning(f"GitHub enrichment failed in quick reply: {e}")

    # 3. Build the Prompt
    prompt = ChatPromptTemplate.from_template(
        """You are writing a short, professional email reply.
        Original email from: {sender}
        Original subject: {subject}
        Original email preview: {original_email_snippet}
        {github_context}
        The user wants to reply with this intent: {intent}
        {calendar_context}
        
        Write a concise, friendly reply. Keep it under 3-4 sentences.
        {scheduling_instruction}
        {github_instruction}
        
        IMPORTANT: Sign the email with the user's name: {user_name}
        Do NOT use placeholders like "[Your Name]". Use the exact name provided.
        Return ONLY a JSON object with: {{"subject": "Re: subject line", "body": "email body"}}
        No markdown."""
    )
    
    scheduling_instruction = ""
    if calendar_context:
        scheduling_instruction = "IMPORTANT: Use the provided calendar availability to suggest specific time slots. Don't just say 'let me know what works' - actually propose 2-3 specific times from the availability."

    github_instruction = ""
    if github_context:
        github_instruction = "IMPORTANT: The email contains a GitHub link. Use the LIVE GITHUB DATA provided above to mention the current status of the issue/PR in your reply (e.g., 'I saw that PR #42 is still open...')."

    try:
        chain = prompt | groq_llm
        result = chain.invoke({
            "sender": sender,
            "subject": subject,
            "intent": intent,
            "original_email_snippet": original_email_snippet[:500],
            "github_context": github_context,
            "calendar_context": calendar_context,
            "scheduling_instruction": scheduling_instruction,
            "github_instruction": github_instruction,
            "user_name": user_name
        })
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Quick reply generation failed: {e}")
        return None