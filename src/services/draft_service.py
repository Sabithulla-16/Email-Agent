from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.core.config import settings
from src.core.logging import logger
from src.db.helpers import get_user_name  # ✅ Import the helper
import json

groq_llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.5,
    groq_api_key=settings.GROQ_API_KEY
)

def generate_email_draft(intent: str, user_uuid: str = None) -> dict | None:
    """Generates an email draft based on user intent and learned preferences."""
    
    user_name = get_user_name(user_uuid) if user_uuid else "Valtry"
    
    # 🔥 Fetch learned preferences
    user_prefs = ""
    if user_uuid:
        try:
            from src.db.client import supabase_client
            user_data = supabase_client.table('users').select('preferences').eq('id', user_uuid).execute()
            if user_data.data and user_data.data[0].get('preferences'):
                user_prefs = f"\n\nUSER STYLE PREFERENCES (Follow these strictly):\n{user_data.data[0]['preferences']}"
        except Exception as e:
            logger.warning(f"Could not fetch preferences: {e}")

    prompt = ChatPromptTemplate.from_template(
        """You are an expert email writer. Based on the user's intent, generate a professional email draft.
        
        The user's name is: {user_name}
        Sign the email with this name, NOT "[Your Name]".
        {user_prefs}
        
        User Intent: {intent}
        
        Return ONLY a valid JSON object with exactly these keys: "to", "subject", "body".
        JSON Output:"""
    )
    
    try:
        chain = prompt | groq_llm
        result = chain.invoke({"intent": intent, "user_name": user_name, "user_prefs": user_prefs})
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Draft generation failed: {e}")
        return None
        
def regenerate_draft_with_edit(current_draft: dict, edit_instruction: str, user_uuid: str = None) -> dict | None:
    """Regenerates the draft based on user's edit instructions."""
    
    # ✅ Use the helper function
    user_name = get_user_name(user_uuid) if user_uuid else "Valtry"
    
    prompt = ChatPromptTemplate.from_template(
        """You are an expert email writer. The user has provided a draft and wants you to edit it based on their instructions.
        
        The user's name is: {user_name}
        Sign the email with this name, NOT "[Your Name]".
        
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
            "edit_instruction": edit_instruction,
            "user_name": user_name
        })
        
        # Clean up markdown formatting if Groq adds it
        clean_text = result.content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"Draft edit failed: {e}")
        return None