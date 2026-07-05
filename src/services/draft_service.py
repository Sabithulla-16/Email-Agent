from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
import json

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.5,
    groq_api_key=settings.GROQ_API_KEY
)

def generate_email_draft(intent: str) -> dict | None:
    """Generates an email draft based on user intent."""
    prompt = ChatPromptTemplate.from_template(
        """You are an expert email writer. Based on the user's intent, generate a professional email draft.
        Return ONLY a valid JSON object with exactly these keys: "to", "subject", "body".
        If the user didn't specify an email address for "to", put "TODO: Add Email".
        
        User Intent: {intent}
        
        JSON Output:"""
    )
    
    try:
        chain = prompt | groq_llm
        result = chain.invoke({"intent": intent})
        
        # Clean up markdown formatting if Groq adds it
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Draft generation failed: {e}")
        return None

def regenerate_draft_with_edit(current_draft: dict, edit_instruction: str) -> dict | None:
    """Regenerates the draft based on user's edit instructions."""
    prompt = ChatPromptTemplate.from_template(
        """You are an expert email writer. The user has provided a draft and wants you to edit it based on their instructions.
        
        CURRENT DRAFT:
        To: {to}
        Subject: {subject}
        Body: {body}
        
        EDIT INSTRUCTION: {edit_instruction}
        
        Return ONLY a valid JSON object with exactly these keys: "to", "subject", "body".
        Apply the edit instruction to improve the draft. Keep the same structure unless the instruction specifically asks to change it.
        
        JSON Output:"""
    )
    
    try:
        chain = prompt | groq_llm
        result = chain.invoke({
            "to": current_draft['to'],
            "subject": current_draft['subject'],
            "body": current_draft['body'],
            "edit_instruction": edit_instruction
        })
        
        # Clean up markdown formatting if Groq adds it
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Draft edit failed: {e}")
        return None