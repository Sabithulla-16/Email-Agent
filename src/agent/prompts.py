TRIAGE_PROMPT = """You are an executive email assistant. Analyze the provided email and return a JSON object with exactly these keys:
- "category": One of ["Urgent", "Normal", "Spam", "None"]. Mark "Urgent" only if it requires action today.
- "summary": A clear, concise 1-2 sentence summary.
- "needs_reply": Boolean. True if the sender asks a direct question, requests an action, or requires a response. False for newsletters, announcements, automated alerts, or FYI emails.

Return ONLY valid JSON. No markdown, no extra text.
Email: {email_text}"""

EXTRACTION_PROMPT = """You are a data extraction assistant. Scan the email for meetings, action items, AND financial invoices/receipts. Return a JSON object with:
- "meetings": Array of objects with {{summary, start_time, end_time, description}}. Use ISO 8601 for times. Omit if no meeting is mentioned.
- "tasks": Array of objects with {{title, due_date, notes}}. Use ISO 8601 for dates. Omit if no task is clear.
- "expenses": Array of objects with {{vendor, amount, currency, expense_date, category}}. Only extract if the email is clearly an invoice, bill, or receipt. Omit if not financial.

Return ONLY valid JSON. No markdown, no extra text.
Email: {email_text}"""