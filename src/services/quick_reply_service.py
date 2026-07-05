from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
import json
from src.tools.calendar_api import get_availability
from datetime import datetime, timedelta

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

# In-memory store for pending quick replies
# Maps a unique reply_id -> {user_uuid, sender, subject, thread_id, message_id, options}
pending_quick_replies: dict = {}

def check_quick_reply(email_text: str, email_id: str, user_uuid: str, sender: str, subject: str, thread_id: str) -> dict | None:
    """
    Determines if an email is a quick-reply candidate.
    Returns quick reply options or None.
    """
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
            # Generate a unique reply ID
            reply_id = f"qr_{email_id}"
            
            # Store in pending quick replies
            pending_quick_replies[reply_id] = {
                'user_uuid': user_uuid,
                'sender': sender,
                'subject': subject,
                'thread_id': thread_id,
                'message_id': email_id,
                'options': data['options']
            }
            
            logger.info(f"⚡ Quick reply candidate detected: {subject}")
            return {'reply_id': reply_id, 'options': data['options']}
        
        return None
    except Exception as e:
        logger.error(f"Quick reply check failed: {e}")
        return None

def generate_quick_reply_email(sender: str, subject: str, intent: str, original_email_snippet: str, user_uuid: str = None) -> dict | None:
    """Generates a short, professional reply email with optional calendar context."""
    
    # 🔥 ENHANCEMENT: Check if this is a scheduling-related intent
    scheduling_keywords = ['time', 'schedule', 'meeting', 'available', 'calendar', 'slot', 'propose']
    is_scheduling = any(keyword in intent.lower() for keyword in scheduling_keywords)

    user_name = "[Your Name]"  # Default fallback
    if user_uuid:
        try:
            from src.db.client import supabase_client
            user_data = supabase_client.table('users').select('full_name').eq('id', user_uuid).execute()
            if user_data.data and user_data.data[0].get('full_name'):
                user_name = user_data.data[0]['full_name']
        except Exception as e:
            logger.warning(f"Could not fetch user name: {e}")
    
    calendar_context = ""
    if is_scheduling and user_uuid:
        try:
            # Get user credentials
            from src.db.client import supabase_client
            from src.tools.google_auth import get_valid_credentials
            
            user_data = supabase_client.table('users').select('google_access_token, google_refresh_token').eq('id', user_uuid).execute()
            if user_data.data:
                creds = get_valid_credentials(user_uuid)
                if creds:
                    # Check availability for next 3 days
                    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
                    availability = get_availability(creds, tomorrow, days=3)
                    
                    if availability:
                        calendar_context = "\n\nUSER'S CALENDAR AVAILABILITY (next 3 days):\n"
                        for date, info in availability.items():
                            if info['free_slots']:
                                calendar_context += f"- {date}: {', '.join(info['free_slots'])}\n"
        except Exception as e:
            logger.warning(f"Could not fetch calendar availability: {e}")
    
    prompt = ChatPromptTemplate.from_template(
        """You are writing a short, professional email reply.
        
        Original email from: {sender}
        Original subject: {subject}
        Original email preview: {original_email_snippet}
        
        The user wants to reply with this intent: {intent}
        {calendar_context}
        
        Write a concise, friendly reply. Keep it under 3-4 sentences.
        {scheduling_instruction}
        
        IMPORTANT: Sign the email with the user's name: {user_name}
        
        Return ONLY a JSON object with: {{"subject": "Re: subject line", "body": "email body"}}
        No markdown."""
    )
    
    scheduling_instruction = ""
    if calendar_context:
        scheduling_instruction = "IMPORTANT: Use the provided calendar availability to suggest specific time slots. Don't just say 'let me know what works' - actually propose 2-3 specific times from the availability."
    
    try:
        chain = prompt | groq_llm
        result = chain.invoke({
            "sender": sender,
            "subject": subject,
            "intent": intent,
            "original_email_snippet": original_email_snippet[:500],
            "calendar_context": calendar_context,
            "scheduling_instruction": scheduling_instruction,
            "user_name": user_name
        })
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Quick reply generation failed: {e}")
        return None