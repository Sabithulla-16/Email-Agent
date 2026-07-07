import re
import asyncio
from datetime import datetime, timezone
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
from src.tools.vacation_mode import check_vacation_mode
from src.services.auto_reply_service import handle_vacation_email

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

def update_crm_and_preferences(user_id: str, sender: str, sentiment: str, edit_instruction: str = None):
    """Updates the Mini CRM and learns from user edits."""
    try:
        # 1. Extract email from sender string (e.g., "John Doe <john@example.com>")
        email_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
        sender_email = email_match.group(0) if email_match else sender
        sender_name = sender.split('<')[0].strip().replace('"', '')

        # 2. Upsert into Contacts table (Mini CRM)
        supabase_client.table('contacts').upsert({
            'user_id': user_id,
            'email': sender_email,
            'name': sender_name,
            'last_sentiment': sentiment,
            'interaction_count': 1, # Will be incremented via RPC or just overwritten for simplicity
            'last_contacted_at': datetime.now(timezone.utc).isoformat()
        }, on_conflict='user_id,email').execute()
        
        logger.info(f"🤝 CRM updated for {sender_email} (Sentiment: {sentiment})")

        # 3. Continuous Learning: If the user edited a draft, save the preference
        if edit_instruction:
            # Fetch current preferences
            user_data = supabase_client.table('users').select('preferences').eq('id', user_id).execute()
            current_prefs = user_data.data[0].get('preferences', '') if user_data.data else ''
            
            # Append the new feedback
            new_prefs = f"{current_prefs} | User feedback: {edit_instruction}".strip(' |')
            
            # Truncate to avoid DB limits
            if len(new_prefs) > 1000:
                new_prefs = new_prefs[-1000:]
                
            supabase_client.table('users').update({'preferences': new_prefs}).eq('id', user_id).execute()
            logger.info(f"🧠 Learned new user preference: {edit_instruction}")
            
    except Exception as e:
        logger.error(f"Failed to update CRM/Preferences: {e}")

async def process_new_email(user_id: str, creds, email_data: dict):
    """Master function to process a single incoming email."""
    logger.info(f"📥 Processing email: {email_data.get('subject')}")

    vacation_info = check_vacation_mode(creds)
    is_vacation = vacation_info.get('is_vacation', False)
    
    if is_vacation:
        logger.info(f"🏖️ User is on vacation until {vacation_info.get('return_date')}")
    
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

    from src.tools.github_api import enrich_email_with_github_data
    safe_text = await enrich_email_with_github_data(safe_text, user_id)

    
    # 2. Run the AI Agent
    initial_state = {
        "email_text": safe_text, 
        "category": None, "summary": None, 
        "meetings": [], "tasks": [], "expenses": [], 
        "needs_reply": False, "is_resolved": False, 
        "sender_sentiment": "Neutral", "error": None
    }

    final_state = email_agent_graph.invoke(initial_state)

    # 2.5 HANDLE VACATION MODE
    if is_vacation:
        category = final_state.get('category', 'Normal')
        is_urgent = category == 'Urgent'
        
        # Send auto-reply
        reply_sent = handle_vacation_email(
            creds=creds,
            user_uuid=user_id,
            email_data=email_data,
            vacation_info=vacation_info,
            category=category,
            is_urgent=is_urgent
        )
        
        # If urgent, notify user via Telegram
        if is_urgent and reply_sent:
            try:
                from src.bot.bot_instance import application
                from src.db.helpers import get_user_telegram_id
                
                telegram_id = get_user_telegram_id(user_id)
                if telegram_id:
                    import html
                    sender_escaped = html.escape(str(email_data.get('sender', 'Unknown')))
                    subject_escaped = html.escape(str(email_data.get('subject', 'No Subject')))
                    
                    msg = f"🚨 <b>URGENT Email During Vacation!</b>\n\n"
                    msg += f"👤 From: {sender_escaped}\n"
                    msg += f"📌 {subject_escaped}\n\n"
                    msg += f"✅ Auto-reply sent. Flagged for your attention upon return."
                    
                    await application.bot.send_message(
                        chat_id=telegram_id,
                        text=msg,
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Failed to send urgent vacation notification: {e}")
    
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

    # 9. THREAD DETECTION

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

    # 10. UPDATE CRM & PREFERENCES
    update_crm_and_preferences(
        user_id=user_id, 
        sender=email_data.get('sender'), 
        sentiment=final_state.get('sender_sentiment', 'Neutral')
    )

        # 🔥 11. HANDLE FORMS & REGISTRATIONS
    for form in final_state.get('forms', []):
        try:
            # 1. Save to DB
            reg_response = supabase_client.table('registrations').insert({
                'user_id': user_id,
                'email_id': saved_email_id,
                'form_url': form.url,
                'form_title': form.title,
                'category': 'General',
                'status': 'Detected'
            }).execute()
            
            if reg_response.data:
                reg_id = reg_response.data[0]['id']
                
                # 2. Notify user via Telegram
                from src.bot.bot_instance import application
                user_response = supabase_client.table('users').select('telegram_id').eq('id', user_id).execute()
                if user_response.data and user_response.data[0].get('telegram_id'):
                    telegram_id = user_response.data[0]['telegram_id']
                    import html
                    
                    title_escaped = html.escape(form.title or 'Unnamed Form')
                    context_escaped = html.escape(form.context or 'No description')
                    url_escaped = html.escape(form.url)
                    
                    msg = f"🔗 <b>Registration/Form Detected!</b>\n\n"
                    msg += f"📌 <b>{title_escaped}</b>\n"
                    msg += f"📝 {context_escaped}\n"
                    msg += f"🌐 <a href='{url_escaped}'>Open Link</a>\n\n"
                    msg += f"Would you like me to auto-fill this form?"
                    
                    keyboard = [
                        [
                            InlineKeyboardButton("🤖 Auto-Fill Form", callback_data=f"reg_autofill_{reg_id}"),
                            InlineKeyboardButton("📋 Save to Tasks", callback_data=f"reg_task_{reg_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Ignore", callback_data=f"reg_ignore_{reg_id}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await application.bot.send_message(
                        chat_id=telegram_id, text=msg, parse_mode='HTML', 
                        reply_markup=reply_markup, disable_web_page_preview=True
                    )
        except Exception as e:
            logger.error(f"Failed to process form: {e}")

    logger.info("🎉 Email processing complete!")
