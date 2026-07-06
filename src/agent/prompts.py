TRIAGE_PROMPT = """You are an executive email assistant. Analyze the provided email thread and return a JSON object with exactly these keys:
- "category": One of ["Urgent", "Normal", "Spam", "None"].
- "summary": A clear, concise 1-2 sentence summary of the LATEST message.
- "needs_reply": Boolean. True if the LATEST message asks a direct question or requires action.
- "is_resolved": Boolean. True if the conversation is completely finished.
- "sender_sentiment": One of ["Positive", "Neutral", "Negative", "Frustrated", "Happy"]. Analyze the tone of the sender.

Return ONLY valid JSON. No markdown, no extra text.
Email Thread: {email_text}"""

forms: List[Form] 