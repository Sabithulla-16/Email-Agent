from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from src.core.config import settings
from src.core.logging import logger
from src.agent.state import AgentState, Meeting, Task, Expense
from src.agent.prompts import TRIAGE_PROMPT, EXTRACTION_PROMPT

def truncate_text(text: str, max_chars: int = 20000) -> str:
    """Truncates text to fit within Groq's token limits (approx 5000 tokens)."""
    if len(text) > max_chars:
        logger.warning(f"🔪 Truncating email text from {len(text)} to {max_chars} characters to avoid Groq limits.")
        return text[:max_chars] + "\n\n[... Content truncated due to length ...]"
    return text

# Initialize Groq LLM
groq_llm = ChatGroq(
    model="openai/gpt-oss-120b", # Extremely fast on Groq
    temperature=0.0,
    groq_api_key=settings.GROQ_API_KEY
)

def triage_node(state: AgentState) -> dict:
    """Categorizes and summarizes the incoming email."""
    logger.info(" Running Triage Node...")
    safe_text = truncate_text(state["email_text"])
    prompt = ChatPromptTemplate.from_template(TRIAGE_PROMPT)
    chain = prompt | groq_llm | JsonOutputParser()
    
    try:
        result = chain.invoke({"email_text": safe_text})
        return {
            "category": result.get("category", "None"),
            "summary": result.get("summary", ""),
            "needs_reply": result.get("needs_reply", False),
            "is_resolved": result.get("is_resolved", False),
            "sender_sentiment": result.get("sender_sentiment", "Neutral")
        }
    except Exception as e:
        logger.error(f"Triage failed: {e}")
        return {"category": "None", "summary": "", "needs_reply": False, "is_resolved": False, "sender_sentiment": "Neutral", "error": str(e)}

def extract_node(state: AgentState) -> dict:
    """Extracts structured meetings, tasks, and expenses."""
    logger.info("🤖 Running Extraction Node...")
    
    # 🔥 TRUNCATE TEXT HERE
    safe_text = truncate_text(state["email_text"])
    
    prompt = ChatPromptTemplate.from_template(EXTRACTION_PROMPT)
    chain = prompt | groq_llm | JsonOutputParser()
    
    try:
        # 🔥 USE safe_text INSTEAD OF state["email_text"]
        result = chain.invoke({"email_text": safe_text})
        
        meetings = [Meeting(**m) for m in result.get("meetings", []) if m.get("summary")]
        tasks = [Task(**t) for t in result.get("tasks", []) if t.get("title")]
        expenses = [Expense(**e) for e in result.get("expenses", []) if e.get("vendor") and e.get("amount")]
        
        return {"meetings": meetings, "tasks": tasks, "expenses": expenses}
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return {"meetings": [], "tasks": [], "expenses": [], "error": str(e)}