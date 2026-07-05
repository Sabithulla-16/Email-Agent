import html
from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram
from src.tools.google_auth import get_valid_credentials
from src.tools.gmail_api import send_email
from src.services.quick_reply_service import pending_quick_replies, generate_quick_reply_email
from src.core.logging import logger
from src.db.client import supabase_client

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    telegram_id = int(query.from_user.id)
    logger.info(f"Button clicked: {callback_data}")
    
    # 🔥 HANDLE QUICK REPLY BUTTONS
    if callback_data.startswith("quick_"):
        await handle_quick_reply_click(query, callback_data)
        return
    
    # HANDLE DRAFT BUTTONS
    if callback_data == "approve_draft":
        draft = context.user_data.get('pending_draft')
        if not draft:
            await query.edit_message_text("❌ Draft expired or not found.")
            return
            
        await query.edit_message_text("📤 Sending email...")
        user_uuid = get_user_uuid_by_telegram(telegram_id)
        creds = get_valid_credentials(user_uuid)
        
        if creds:
            message_id = send_email(creds, draft['to'], draft['subject'], draft['body'])
            if message_id:
                await query.edit_message_text(f"✅ Email successfully sent to {draft['to']}!")
            else:
                await query.edit_message_text("❌ Failed to send email.")
        else:
            await query.edit_message_text("❌ Google session expired. Please use /start.")
        context.user_data.pop('pending_draft', None)
        
    elif callback_data == "edit_draft":
        context.user_data['awaiting_draft_edit'] = True
        await query.edit_message_text(
            "✏️ <b>Edit Mode Activated</b>\n\n"
            "Please tell me what you'd like to change.\n"
            "Examples:\n"
            "- 'Change the signature to John Doe'\n"
            "- 'Make the tone more formal'",
            parse_mode='HTML'
        )
        
    elif callback_data == "cancel_draft":
        context.user_data.pop('pending_draft', None)
        await query.edit_message_text("❌ Email draft cancelled.")


async def handle_quick_reply_click(query, callback_data: str):
    """Handles quick reply button clicks."""
    # Parse callback: quick_qr_{email_id}_{option_index}
    parts = callback_data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("❌ Invalid quick reply.")
        return
    
    # Reconstruct reply_id (it's qr_{email_id})
    option_index = int(parts[-1])
    reply_id = "_".join(parts[1:-1])
    
    # Look up the pending quick reply
    quick_reply = pending_quick_replies.get(reply_id)
    if not quick_reply:
        await query.edit_message_text("❌ This quick reply has expired or was already used.")
        return
    
    if option_index >= len(quick_reply['options']):
        await query.edit_message_text("❌ Invalid option.")
        return
    
    selected_option = quick_reply['options'][option_index]
    await query.edit_message_text(f"✍️ Generating reply: '{selected_option['label']}'...")
    
    # Generate the reply email
    reply_email = generate_quick_reply_email(
        sender=quick_reply['sender'],
        subject=quick_reply['subject'],
        intent=selected_option['intent'],
        original_email_snippet="",
        user_uuid=quick_reply['user_uuid']
    )
    
    if not reply_email:
        await query.edit_message_text("❌ Failed to generate reply. Please try again.")
        return
    
    # Get credentials and send
    creds = get_valid_credentials(quick_reply['user_uuid'])
    if not creds:
        await query.edit_message_text("❌ Google session expired. Please use /start.")
        return
    
    message_id = send_email(
        creds, 
        quick_reply['sender'], 
        reply_email['subject'], 
        reply_email['body']
    )
    
    if message_id:
        # 🔥 FIX: Escape the sender variable so the < > don't break HTML parsing
        sender_escaped = html.escape(str(quick_reply['sender']))
        
        await query.edit_message_text(
            f"✅ Reply sent to {sender_escaped}!\n\n"
            f"<b>Subject:</b> {html.escape(reply_email['subject'])}\n"
            f"<b>Body:</b> {html.escape(reply_email['body'][:200])}...",
            parse_mode='HTML'
        )

        try:
            supabase_client.table('emails').update({
                'reply_status': 'Replied'
            }).eq('message_id', quick_reply['message_id']).execute()
            logger.info(f"✅ Marked email {quick_reply['message_id']} as Replied.")
        except Exception as db_err:
            logger.error(f"Failed to update reply status: {db_err}")

    else:
        await query.edit_message_text("❌ Failed to send reply.")
    
    # Remove from pending (one-time use)
    pending_quick_replies.pop(reply_id, None)