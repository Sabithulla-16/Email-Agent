import asyncio
from src.agent.graph import email_agent_graph
from src.services.security import redact_pii
from src.services.rag_service import generate_embedding
from src.tools.calendar_api import create_calendar_event
from src.tools.tasks_api import create_task
from src.tools.drive_api import get_attachment_content
from src.db.client import supabase_client
from src.core.logging import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from langchain.text_splitter import RecursiveCharacterTextSplitter
from src.services.quick_reply_service import check_quick_reply
from src.tools.gmail_api import archive_thread

def get_thread_history(user_id: str, thread_id: str) -> str:
    """Fetches previous emails in the same thread from the database."""
    if not thread_id:
        return ""
        
    try:
        # Fetch older emails in this thread, ordered by received date
        response = supabase_client.table('emails').select('sender, body_text').eq(
            'user_id', user_id
        ).eq(
            'thread_id', thread_id
        ).order('received_at', desc=False).execute() # Oldest first
        
        if not response.data:
            return ""
            
        history_text = "\n\n--- PREVIOUS MESSAGES IN THIS THREAD ---\n"
        for msg in response.data:
            history_text += f"From: {msg['sender']}\n{msg['body_text'][:1000]}\n\n"
            
        return history_text
    except Exception as e:
        logger.error(f"Failed to fetch thread history: {e}")
        return ""

async def process_new_email(user_id: str, creds, email_data: dict):
    """Master function to process a single incoming email."""
    logger.info(f"📥 Processing email: {email_data.get('subject')}")
    
    # 1. Combine text and Redact PII
    raw_text = f"Subject: {email_data.get('subject')}\nFrom: {email_data.get('sender')}\n{email_data.get('body_text', email_data.get('snippet'))}"
    
    thread_history = get_thread_history(user_id, email_data.get('threadId'))
    if thread_history:
        raw_text += thread_history
        logger.info(f"🧵 Included previous thread history for: {email_data.get('subject')}")

    # Check for and extract attachments
    attachment_text = get_attachment_content(creds, email_data.get('id'))
    if attachment_text:
        raw_text += f"\n\n--- ATTACHED DOCUMENTS ---\n{attachment_text}"
        logger.info(f"📎 Found and extracted text from attachments for: {email_data.get('subject')}")
        
    safe_text = redact_pii(raw_text)
    
    # 2. Run the AI Agent
    initial_state = {
        "email_text": safe_text, 
        "category": None, "summary": None, 
        "meetings": [], "tasks": [], "expenses": [], "needs_reply": False, "error": None
    }
    final_state = email_agent_graph.invoke(initial_state)
    
    # 3. Generate Main Embedding (for the overall email)
    main_embedding = generate_embedding(safe_text)
    
    # 4. Save to Supabase 'emails' table
    email_record = {
        "user_id": user_id,
        "message_id": email_data.get('id'),
        "thread_id": email_data.get('threadId'),
        "subject": email_data.get('subject'),
        "sender": email_data.get('sender'),
        "snippet": email_data.get('snippet'),
        "body_text": raw_text, 
        "category": final_state.get('category'),
        "summary": final_state.get('summary'),
        "embedding": main_embedding,
        "needs_reply": final_state.get('needs_reply', False)
    }
    
    db_response = supabase_client.table('emails').insert(email_record).execute()
    if not db_response.data:
        logger.error("Failed to save email to database.")
        return
        
    saved_email_id = db_response.data[0]['id']
    logger.info(f"✅ Email saved to DB with ID: {saved_email_id}")

    # 🔥 4.5. DOCUMENT CHUNKING (Advanced RAG)
    # We only chunk if the text is long enough to warrant it
    if len(safe_text) > 500:
        try:
            # Split text into 500-character chunks with 50-character overlap
            # 500 chars is roughly 125 tokens, perfectly safe for our embedding model
            splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
            chunks = splitter.split_text(safe_text)
            
            chunk_records = []
            for i, chunk in enumerate(chunks):
                chunk_embedding = generate_embedding(chunk)
                chunk_records.append({
                    "email_id": saved_email_id,
                    "user_id": user_id,
                    "chunk_text": chunk,
                    "chunk_index": i,
                    "embedding": chunk_embedding
                })
            
            if chunk_records:
                supabase_client.table('email_chunks').insert(chunk_records).execute()
                logger.info(f"🧩 Saved {len(chunk_records)} text chunks for email {saved_email_id}")
        except Exception as e:
            logger.error(f"Chunking failed: {e}")

    # 5. Create Calendar Events if extracted
    for meeting in final_state.get('meetings', []):
        if meeting.start_time and meeting.end_time:
            event_id = create_calendar_event(
                creds, meeting.summary, meeting.start_time, meeting.end_time, meeting.description
            )
            if event_id:
                supabase_client.table('calendar_mappings').insert({
                    "email_id": saved_email_id, "google_event_id": event_id
                }).execute()
                
    # 6. Create Tasks if extracted
    for task in final_state.get('tasks', []):
        task_id = create_task(creds, task.title, task.notes, task.due_date)
        if task_id:
            supabase_client.table('task_mappings').insert({
                "email_id": saved_email_id, "google_task_id": task_id
            }).execute()

    # 🔥 7. Save Expenses (WITH DEDUPLICATION)
    for expense in final_state.get('expenses', []):
        try:
            # Check if this exact expense already exists for this user
            existing = supabase_client.table('expenses').select('id').eq('user_id', user_id).eq('vendor', expense.vendor).eq('amount', expense.amount).eq('expense_date', expense.expense_date).execute()
            
            if existing.data:
                logger.info(f"️ Duplicate expense skipped: {expense.vendor} - {expense.amount} on {expense.expense_date}")
                continue
                
            supabase_client.table('expenses').insert({
                "user_id": user_id,
                "email_id": saved_email_id,
                "vendor": expense.vendor,
                "amount": expense.amount,
                "currency": expense.currency,
                "expense_date": expense.expense_date,
                "category": expense.category
            }).execute()
            logger.info(f"💰 Saved unique expense: {expense.vendor} - {expense.amount} {expense.currency}")
        except Exception as e:
            logger.error(f"Failed to save expense: {e}")

    # 8. CHECK FOR QUICK REPLY
    if final_state.get('category') not in ['Spam', 'None']:
        quick_reply = check_quick_reply(
            email_text=safe_text,
            email_id=email_data.get('id'),
            user_uuid=user_id,
            sender=email_data.get('sender'),
            subject=email_data.get('subject'),
            thread_id=email_data.get('threadId')
        )
        
        if quick_reply:
            from src.bot.bot_instance import application
            user_response = supabase_client.table('users').select('telegram_id').eq('id', user_id).execute()
            if user_response.data and user_response.data[0].get('telegram_id'):
                import html
                telegram_id = user_response.data[0]['telegram_id']
                
                summary = final_state.get('summary', email_data.get('snippet', ''))
                sender_escaped = html.escape(str(email_data.get('sender', 'Unknown')))
                subject_escaped = html.escape(str(email_data.get('subject', 'No Subject')))
                summary_escaped = html.escape(str(summary))

                msg = f"⚡ <b>Quick Reply Needed!</b>\n\n"
                msg += f" From: {sender_escaped}\n"
                msg += f"📌 {subject_escaped}\n\n"
                msg += f"{summary_escaped}\n\n"
                msg += f" Choose a quick reply:"
                
                buttons = []
                for option in quick_reply['options']:
                    callback_data = f"quick_{quick_reply['reply_id']}_{quick_reply['options'].index(option)}"
                    buttons.append([InlineKeyboardButton(option['label'], callback_data=callback_data)])
                
                reply_markup = InlineKeyboardMarkup(buttons)
                
                try:
                    await application.bot.send_message(
                        chat_id=telegram_id, text=msg, parse_mode='HTML', reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Failed to send quick reply notification: {e}")

    if final_state.get('is_resolved') and email_data.get('threadId'):
        logger.info(f"✅ Thread detected as resolved. Archiving in Gmail...")
        archive_thread(creds, email_data.get('threadId'))
        
        # Also update our database so we don't send follow-up reminders for it
        try:
            supabase_client.table('emails').update({
                'reply_status': 'Resolved'
            }).eq('message_id', email_data.get('id')).execute()
        except Exception as db_err:
            logger.error(f"Failed to update DB status to Resolved: {db_err}")

    logger.info("🎉 Email processing complete!")
