from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import supabase_client
from src.tools.google_auth import get_auth_url
from src.tools.gmail_api import fetch_latest_emails
from src.tools.google_auth import get_valid_credentials
from src.services.rag_service import chat_with_emails
from src.tools.calendar_api import get_todays_events
from src.tools.tasks_api import get_pending_tasks
from src.db.client import get_user_uuid_by_telegram
from src.services.rag_service import chat_with_emails
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.core.logging import logger
from src.services.briefing_service import generate_briefing
from src.services.ingestion import process_new_email
from src.tools.gmail_api import get_gmail_service
from datetime import datetime, timedelta
from src.bot.utils import send_formatted_message, send_html_to_chat
import base64
import asyncio

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command - initiates Google OAuth flow."""
    # Get the actual Telegram user ID
    telegram_id = str(update.effective_user.id)
    logger.info(f"User {telegram_id} initiated /start")
    
    # Pass the telegram_id to get_auth_url
    auth_url = get_auth_url(telegram_id=telegram_id)
    
    await update.message.reply_text(
        f"👋 Welcome to your Email Agent!\n\n"
        f"To get started, please connect your Google account:\n\n"
        f"🔗 [Click here to authorize]({auth_url})\n\n"
        f"After authorizing, you'll see a success message. Then you can use:\n"
        f"/recent - View recent emails\n"
        f"/search <query> - Search your emails with AI",
        parse_mode='Markdown'
    )

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /recent command - shows recent emails."""
    telegram_id = int(update.effective_user.id)
    await update.message.reply_text("📧 Fetching your recent emails...")
    
    # 1. Find the user in Supabase
    response = supabase_client.table('users').select('id').eq('telegram_id', telegram_id).execute()
    if not response.data:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    user_uuid = response.data[0]['id']
    
    # 2. Get Google credentials
    creds = get_valid_credentials(user_uuid)
    if not creds:
        await update.message.reply_text("❌ Could not retrieve Google credentials. Please use /start to log in again.")
        return
        
    # 3. Fetch the latest 5 emails (now sorted by date!)
    emails = fetch_latest_emails(creds, max_results=5)
    
    if not emails:
        await update.message.reply_text("📭 Your inbox is empty or I couldn't fetch any emails.")
        return
        
    # 4. Format the emails for Telegram (including the time!)
    message_text = "📬 *Here are your 5 most recent emails (Newest First):*\n\n"
    for i, email in enumerate(emails, 1):
        message_text += f"👉 {i}. {email['subject']}\n"
        message_text += f"   👤 From: {email['sender']}\n"
        # Add the time here!
        message_text += f"   🕒 Time: {email.get('readable_date', 'Unknown')}\n"
        message_text += f"   📝 {email['snippet']}\n\n"
        
    await update.message.reply_text(message_text)

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /tasks command - shows pending tasks."""
    telegram_id = int(update.effective_user.id)
    await update.message.reply_text("📋 Fetching your pending tasks...")
    
    # Find user and get credentials
    response = supabase_client.table('users').select('id').eq('telegram_id', telegram_id).execute()
    if not response.data:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    user_uuid = response.data[0]['id']
    creds = get_valid_credentials(user_uuid)
    if not creds:
        await update.message.reply_text("❌ Could not retrieve Google credentials. Please use /start to log in again.")
        return
    
    # Fetch tasks
    tasks = get_pending_tasks(creds)
    
    if not tasks:
        await update.message.reply_text("✅ You have no pending tasks! Great job!")
        return
    
    # Format tasks for Telegram
    message_text = "📋 *Your Pending Tasks:*\n\n"
    for i, task in enumerate(tasks, 1):
        message_text += f"👉 {i}. {task['title']}\n"
        if task['due_date']:
            message_text += f"   📅 Due: {task['due_date']}\n"
        if task['notes']:
            message_text += f"   📝 {task['notes'][:100]}...\n"
        message_text += "\n"
    
    await update.message.reply_text(message_text)

async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /calendar command - shows today's events."""
    telegram_id = int(update.effective_user.id)
    await update.message.reply_text("📅 Fetching today's events...")
    
    # Find user and get credentials
    response = supabase_client.table('users').select('id').eq('telegram_id', telegram_id).execute()
    if not response.data:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    user_uuid = response.data[0]['id']
    creds = get_valid_credentials(user_uuid)
    if not creds:
        await update.message.reply_text("❌ Could not retrieve Google credentials. Please use /start to log in again.")
        return
    
    # Fetch events
    events = get_todays_events(creds)
    
    if not events:
        await update.message.reply_text("📅 You have no events scheduled for today!")
        return
    
    # Format events for Telegram
    message_text = "📅 *Today's Schedule:*\n\n"
    for i, event in enumerate(events, 1):
        message_text += f"👉 {i}. {event['summary']}\n"
        message_text += f"   🕒 Time: {event['start_time']}\n"
        if event['description']:
            message_text += f"   📝 {event['description'][:100]}...\n"
        message_text += "\n"
    
    await update.message.reply_text(message_text)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /search command - AI-powered email search."""
    if not context.args:
        await update.message.reply_text("Please provide a search query. Example: /search payment terms")
        return
    
    query = " ".join(context.args)
    telegram_id = int(update.effective_user.id)
    
    await update.message.reply_text(f"🔍 Searching your emails for: '{query}'...")
    
    # Get actual user UUID
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    answer = await chat_with_emails(user_id=user_uuid, question=query)
    await update.message.reply_text(answer)

async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /draft command - starts the email drafting flow."""
    telegram_id = int(update.effective_user.id)
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return

    # Set the state flag so the next message goes to the draft generator
    context.user_data['awaiting_draft_intent'] = True
    
    await update.message.reply_text(
        "✍️ *Let's draft an email!*\n\n"
        "Please tell me who it's to and what you want to say.\n"
        "Example: 'Send an email to john@test.com saying the project is delayed by 2 days.'",
        parse_mode='Markdown'
    )

async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /briefing command - generates a daily summary."""
    telegram_id = int(update.effective_user.id)
    await update.message.reply_text("☀️ Generating your daily briefing...")
    
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    creds = get_valid_credentials(user_uuid)
    if not creds:
        await update.message.reply_text("❌ Could not retrieve Google credentials.")
        return
        
    briefing_text = await generate_briefing(user_uuid, creds)
    await send_formatted_message(update, briefing_text)

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /sync command - fetches and stores recent emails in database."""
    telegram_id = int(update.effective_user.id)
    
    # Check if user is logged in first
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    creds = get_valid_credentials(user_uuid)
    if not creds:
        await update.message.reply_text("❌ Could not retrieve Google credentials. Please use /start to log in again.")
        return

    # Immediately reply to Telegram to prevent timeout
    await update.message.reply_text("🔄 Syncing your emails to the database... This may take a couple of minutes. I'll notify you when it's done!")
    
    # Run the heavy sync process in the background
    asyncio.create_task(background_sync(telegram_id, user_uuid, creds))

async def background_sync(telegram_id: int, user_uuid: str, creds):
    """Runs the heavy email syncing process in the background."""
    try:
        service = get_gmail_service(creds)
        results = service.users().messages().list(
            userId='me', 
            maxResults=20, 
            labelIds=['INBOX'], 
            q='category:primary' 
        ).execute()
        messages = results.get('messages', [])
        
        if not messages:
            from src.bot.bot_instance import application
            await application.bot.send_message(chat_id=telegram_id, text="📭 No emails found to sync.")
            return
        
        synced_count = 0
        for msg in messages:
            try:
                # Skip if already synced
                existing = supabase_client.table('emails').select('id').eq('message_id', msg['id']).execute()
                if existing.data:
                    continue  
                
                # Fetch full email details
                msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
                
                # Extract headers
                headers = msg_data['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown Sender')
                
                # Extract body text
                body_text = ""
                if 'parts' in msg_data['payload']:
                    for part in msg_data['payload']['parts']:
                        if part['mimeType'] == 'text/plain':
                            body_text = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                            break
                elif 'data' in msg_data['payload'].get('body', {}):
                    body_text = base64.urlsafe_b64decode(msg_data['payload']['body']['data']).decode('utf-8')
                
                email_data = {
                    'id': msg['id'],
                    'threadId': msg_data.get('threadId'),
                    'subject': subject,
                    'sender': sender,
                    'snippet': msg_data.get('snippet', ''),
                    'body_text': body_text
                }
                
                # Process and store the email
                await process_new_email(user_uuid, creds, email_data)
                synced_count += 1
                
            except Exception as e:
                logger.error(f"Error syncing email {msg['id']}: {e}")
                continue
        
        # Notify the user that sync is complete using the bot instance directly
        from src.bot.bot_instance import application
        await application.bot.send_message(
            chat_id=telegram_id, 
            text=f"✅ Successfully synced {synced_count} new emails to the database!\n\nYou can now use /search or ask questions about your emails."
        )
        
    except Exception as e:
        logger.error(f"Background sync failed: {e}")
        from src.bot.bot_instance import application
        await application.bot.send_message(
            chat_id=telegram_id,
            text="❌ An error occurred while syncing emails. Please check the logs."
        )

async def expenses_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /expenses command - shows spending summary."""
    telegram_id = int(update.effective_user.id)
    
    # Check if user is logged in
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return

    await update.message.reply_text("💰 Calculating your expenses...")

    # Determine the date range (default to current month)
    now = datetime.now()
    start_of_month = now.replace(day=1).strftime('%Y-%m-%d')
    
    # Fetch expenses from Supabase for the current month
    response = supabase_client.table('expenses').select(
        'vendor, amount, currency, expense_date, category'
    ).eq(
        'user_id', user_uuid
    ).gte(
        'expense_date', start_of_month
    ).order('expense_date', desc=True).execute()

    expenses = response.data

    if not expenses:
        await update.message.reply_text(f" No expenses recorded for this month (since {start_of_month}).")
        return

    # Calculate total and format the message
    total = sum(e['amount'] for e in expenses)
    currency = expenses[0]['currency'] # Assuming single currency for simplicity
    
    msg = f"💰 <b>Expenses for {now.strftime('%B %Y')}</b>\n\n"
    
    # Group by category
    categories = {}
    for e in expenses:
        cat = e['category'] or 'Uncategorized'
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(e)

    for cat, items in categories.items():
        msg += f"<b>{cat}:</b>\n"
        for item in items:
            msg += f"  - {item['vendor']}: {item['amount']} {item['currency']} ({item['expense_date']})\n"
        msg += "\n"
        
    msg += f"--------------------------\n"
    msg += f"<b>Total Spent: {total:.2f} {currency}</b>"

    await update.message.reply_text(msg, parse_mode='HTML')

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /profile command - manages auto-fill profile."""
    telegram_id = int(update.effective_user.id)
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return

    if not context.args:
        # Show current profile
        profile_data = supabase_client.table('user_profiles').select('*').eq('user_id', user_uuid).execute()
        if profile_data.data:
            p = profile_data.data[0]
            msg = "👤 <b>Your Auto-Fill Profile</b>\n\n"
            for k, v in p.items():
                if k not in ['id', 'user_id', 'updated_at'] and v:
                    msg += f"<b>{k.replace('_', ' ').title()}:</b> {v}\n"
            msg += "\n<i>Use /profile set [field] [value] to update.</i>\n"
            msg += "<i>Example: /profile set github https://github.com/valtry</i>"
            await update.message.reply_text(msg, parse_mode='HTML')
        else:
            await update.message.reply_text("👤 No profile set yet. Use /profile set [field] [value] to start.\nExample: /profile set name Valtry")
        return

    if context.args[0].lower() == 'set' and len(context.args) >= 3:
        field = context.args[1].lower()
        value = " ".join(context.args[2:])
        
        field_map = {
            'name': 'full_name', 'email': 'email', 'phone': 'phone',
            'github': 'github_link', 'linkedin': 'linkedin_link',
            'resume': 'resume_link', 'college': 'college_name', 'team': 'team_name'
        }
        db_field = field_map.get(field, field)
        
        supabase_client.table('user_profiles').upsert({
            'user_id': user_uuid,
            db_field: value
        }, on_conflict='user_id').execute()
        
        await update.message.reply_text(f"✅ Updated <b>{db_field.replace('_', ' ').title()}</b> to:\n{value}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Invalid format. Use: /profile set [field] [value]")