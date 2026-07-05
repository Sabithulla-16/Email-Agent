import re
from src.core.logging import logger

def redact_pii(text: str) -> str:
    """
    Masks sensitive PII (Personally Identifiable Information) using regex.
    """
    if not text:
        return ""

    # Redact Social Security Numbers (XXX-XX-XXXX)
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[REDACTED SSN]', text)
    
    # Redact Credit Card Numbers (13 to 16 digits, with optional spaces/dashes)
    text = re.sub(r'\b(?:\d[ -]*?){13,16}\b', '[REDACTED CARD]', text)
    
    # Note: We intentionally do NOT redact email addresses here because 
    # we want the LLM to know who sent the email for context.
    
    return text