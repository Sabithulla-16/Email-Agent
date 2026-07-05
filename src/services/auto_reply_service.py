from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
from src.db.helpers import get_user_name
from src.tools.gmail_api import send_email, archive_thread
import json

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.3,
    groq_api_key=settings.GROQ_API_KEY
)

def generate_vacation_reply(
    sender: str, 
    subject: str, 
    email_body: str, 
    return_date: str, 
    user_uuid: str,
    is_urgent: bool = False
) -> dict | None:
    """
    Generates a context-aware vacation auto-reply.
    """
    user_name = get_user_name(user_uuid) if user_uuid else "Valtry"
    
    prompt = ChatPromptTemplate.from_template(
        """You are an AI assistant managing emails while the user is on vacation.
        
        The user's name is: {user_name}
        The user is away until: {return_date}
        
        Incoming email details:
        From: {sender}
        Subject: {subject}
        Body: {email_body}
        
        Is this urgent? {is_urgent}
        
        Generate a professional, friendly auto-reply that:
        1. Acknowledges their email
        2. Mentions that {user_name} is currently out of office
        3. States the return date ({return_date})
        4. If urgent, mentions that the message has been flagged for immediate attention upon return
        5. If not urgent, mentions that the email will be reviewed upon return
        6. Signs off with {user_name}'s name
        
        Keep it concise (3-4 sentences max).
        
        Return ONLY a JSON object with: {{"subject": "Re: subject line", "body": "email body"}}
        No markdown."""
    )
    
    try:
        chain = prompt | groq_llm
        result = chain.invoke({
            "sender": sender,
            "subject": subject,
            "email_body": email_body[:500],
            "return_date": return_date,
            "user_name": user_name,
            "is_urgent": "Yes" if is_urgent else "No"
        })
        
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Vacation reply generation failed: {e}")
        return None

def handle_vacation_email(
    creds, 
    user_uuid: str, 
    email_data: dict, 
    vacation_info: dict,
    category: str,
    is_urgent: bool = False
) -> bool:
    """
    Handles an incoming email during vacation mode.
    Returns True if auto-reply was sent.
    """
    sender = email_data.get('sender', '')
    subject = email_data.get('subject', '')
    body = email_data.get('body_text', '') or email_data.get('snippet', '')
    thread_id = email_data.get('threadId')
    return_date = vacation_info.get('return_date', 'soon')
    
    # 1. Auto-archive spam and newsletters
    if category in ['Spam', 'None'] or 'unsubscribe' in body.lower():
        logger.info(f"🗄️ Vacation mode: Auto-archiving spam/newsletter from {sender}")
        if thread_id:
            archive_thread(creds, thread_id)
        return False
    
    # 2. Generate and send auto-reply
    logger.info(f"🏖️ Vacation mode: Generating auto-reply for {sender}")
    
    reply = generate_vacation_reply(
        sender=sender,
        subject=subject,
        email_body=body,
        return_date=return_date,
        user_uuid=user_uuid,
        is_urgent=is_urgent
    )
    
    if reply:
        # Extract email address from sender
        import re
        email_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
        sender_email = email_match.group(0) if email_match else sender
        
        # Send the reply
        message_id = send_email(creds, sender_email, reply['subject'], reply['body'])
        
        if message_id:
            logger.info(f"✅ Vacation auto-reply sent to {sender_email}")
            return True
        else:
            logger.error(f"❌ Failed to send vacation auto-reply")
            return False
    else:
        logger.error(f"❌ Failed to generate vacation reply")
        return False