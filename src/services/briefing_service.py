from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.tools.gmail_api import fetch_latest_emails
from src.tools.calendar_api import get_todays_events
from src.tools.tasks_api import get_pending_tasks
from src.db.client import supabase_client
from src.core.logging import logger

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.3,
    groq_api_key=settings.GROQ_API_KEY
)

async def generate_briefing(user_uuid: str, creds) -> str:
    """Generates a consolidated daily briefing."""
    # 1. Fetch Data
    emails = fetch_latest_emails(creds, max_results=5)
    events = get_todays_events(creds)
    tasks = get_pending_tasks(creds)
    
    # 2. Format Data for LLM
    email_str = "\n".join([f"- {e['subject']} from {e['sender']}" for e in emails]) or "None"
    event_str = "\n".join([f"- {ev['start_time']}: {ev['summary']}" for ev in events]) or "None"
    task_str = "\n".join([f"- {t['title']}" for t in tasks]) or "None"
    
    # 3. Prompt Groq
    prompt = ChatPromptTemplate.from_template(
        """You are an executive assistant. Format the following data into a clean, motivating, 
        and easy-to-read daily morning briefing.
        
        FORMATTING RULES:
        - You are generating HTML for Telegram. Telegram ONLY supports these tags: <b>, <i>, <ul>, <ol>, <li>.
        - Use <b> for headers and important text.
        - Use <ul><li>item</li></ul> for lists.
        - Use \n for line breaks and spacing.
        - DO NOT use <p>, <br>, <h1>, <h2>, <div>, or any other HTML tags.
        - DO NOT use markdown symbols like #, *, or _.
        - DO NOT use the & symbol, write "and" instead.
        
        TODAY'S SCHEDULE:
        {events}
        
        PENDING TASKS:
        {tasks}
        
        RECENT EMAILS:
        {emails}
        
        Briefing:"""
    )
    
    chain = prompt | groq_llm
    result = chain.invoke({"events": event_str, "tasks": task_str, "emails": email_str})
    
    return result.content