from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from src.db.client import supabase_client
from src.tools.google_auth import get_valid_credentials
from src.services.briefing_service import generate_briefing
from src.core.logging import logger
from src.bot.bot_instance import application
from src.bot.utils import send_html_to_chat
from datetime import datetime, timedelta, timezone
import html

# Initialize the background scheduler
scheduler = AsyncIOScheduler()

async def send_morning_briefings():
    """Fetches all users and sends them their daily briefing."""
    logger.info("🌅 Starting daily morning briefings...")
    
    # Fetch all users who have a telegram_id linked
    response = supabase_client.table('users').select('id, telegram_id').not_.is_('telegram_id', 'null').execute()
    users = response.data

    for user in users:
        telegram_id = user['telegram_id']
        user_uuid = user['id']
        
        try:
            # 1. Get valid Google credentials
            creds = get_valid_credentials(user_uuid)
            if not creds:
                logger.warning(f"No valid credentials for user {telegram_id}. Skipping briefing.")
                continue
            
            # 2. Generate the briefing
            logger.info(f"📝 Generating briefing for user {telegram_id}...")
            briefing_text = await generate_briefing(user_uuid, creds)
            
            # 3. Send it via Telegram
            await send_html_to_chat(
                chat_id=telegram_id,
                text=f"☀️ <b>Good Morning!</b> Here is your daily briefing:\n\n{briefing_text}",
                bot=application.bot
            )
            logger.info(f"✅ Successfully sent briefing to {telegram_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to send briefing to {telegram_id}: {e}")

async def check_pending_replies():
    """Checks for emails that need a reply but haven't been answered in 48 hours."""
    logger.info("🔔 Checking for pending email replies...")
    
    # Calculate the threshold (48 hours ago)
    threshold_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    
    try:
        # Fetch emails that need a reply, are still pending, and are older than 48h
        # We also join with the users table to get the telegram_id
        response = supabase_client.table('emails').select(
            'id, subject, sender, snippet, user_id, users!inner(telegram_id)'
        ).eq(
            'needs_reply', True
        ).eq(
            'reply_status', 'Pending'
        ).lte(
            'received_at', threshold_time
        ).execute()
        
        pending_emails = response.data
        if not pending_emails:
            logger.info("✅ No pending replies found.")
            return
            
        # Group by user so we send one consolidated message per user
        user_reminders = {}
        for email in pending_emails:
            tg_id = email['users']['telegram_id']
            if tg_id not in user_reminders:
                user_reminders[tg_id] = []
            user_reminders[tg_id].append(email)
            
        # Send reminders
        for tg_id, emails in user_reminders.items():
            msg = "🔔 <b>Follow-up Reminder!</b>\n\nYou have pending emails that need a reply:\n\n"
            for email in emails[:5]: # Limit to 5 per message to avoid spam
                msg += f"👤 <b>{html.escape(email['sender'])}</b>\n"
                msg += f"📌 {html.escape(email['subject'])}\n"
                msg += f"📝 {html.escape(email['snippet'][:100])}...\n\n"
                
            if len(emails) > 5:
                msg += f"<i>...and {len(emails) - 5} more.</i>\n"
                
            msg += "\nOpen Gmail to reply, or use the Quick Reply buttons next time!"
            
            try:
                await send_html_to_chat(
                    chat_id=tg_id,
                    text=msg,
                    bot=application.bot
                )
                logger.info(f"✅ Sent follow-up reminder to {tg_id} for {len(emails)} emails.")
            except Exception as e:
                logger.error(f"Failed to send reminder to {tg_id}: {e}")
                
    except Exception as e:
        logger.error(f"Error checking pending replies: {e}")

def start_scheduler():
    """Starts the background scheduler."""
    # Schedule the job for 8:00 AM every day. 
    # Note: This uses your server's local time. If you need a specific timezone, 
    # you can add timezone='Asia/Kolkata' (or your timezone) to CronTrigger.
    scheduler.add_job(
        send_morning_briefings,
        trigger=CronTrigger(hour=8, minute=0), 
        id="morning_briefing",
        name="Daily Morning Briefing",
        replace_existing=True
    )

    scheduler.add_job(
        check_pending_replies,
        trigger=CronTrigger(hour=10, minute=0), 
        id="follow_up_reminders",
        name="Follow-up Email Reminders",
        replace_existing=True
    )

    scheduler.start()
    logger.info("✅ Background scheduler started. Morning briefings at 8:00 AM, Follow-ups at 10:00 AM.")

def shutdown_scheduler():
    """Shuts down the scheduler gracefully."""
    scheduler.shutdown()
    logger.info("🛑 Background scheduler stopped.")