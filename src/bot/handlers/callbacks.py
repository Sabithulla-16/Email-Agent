import html
from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram
from src.tools.google_auth import get_valid_credentials
from src.tools.gmail_api import send_email
from src.services.quick_reply_service import pending_quick_replies, generate_quick_reply_email
from src.core.logging import logger
from src.db.client import supabase_client
from src.services.form_filler_service import analyze_and_fill_form, submit_form, cancel_form

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

        # 🔥 HANDLE REGISTRATION BUTTONS
    if callback_data.startswith("reg_autofill_"):
        reg_id = callback_data.split("_")[2]
        await handle_registration_autofill(query, context, reg_id)
        return
    if callback_data.startswith("reg_task_"):
        reg_id = callback_data.split("_")[2]
        await handle_registration_to_task(query, reg_id)
        return
    if callback_data.startswith("reg_ignore_"):
        reg_id = callback_data.split("_")[2]
        supabase_client.table('registrations').update({'status': 'Ignored'}).eq('id', reg_id).execute()
        await query.edit_message_text("❌ Registration ignored.")
        return
    if callback_data.startswith("reg_proceed_"):
        reg_id = callback_data.split("_")[2]
        await handle_registration_proceed(query, reg_id)
        return
    if callback_data.startswith("reg_edit_"):
        reg_id = callback_data.split("_")[2]
        await handle_registration_edit(query, context, reg_id)
        return
    if callback_data.startswith("reg_cancel_"):
        reg_id = callback_data.split("_")[2]
        await handle_registration_cancel(query, reg_id)
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

async def handle_registration_autofill(query, context, reg_id: str):
    """Starts the auto-fill process."""
    await query.edit_message_text("🤖 <b>Opening browser and analyzing form...</b>\n\n<i>This may take 10-20 seconds.</i>", parse_mode='HTML')
    
    reg_data = supabase_client.table('registrations').select('*').eq('id', reg_id).execute()
    if not reg_data.data:
        await query.edit_message_text("❌ Registration not found.")
        return
    
    reg = reg_data.data[0]
    user_uuid = reg['user_id']
    form_url = reg['form_url']
    
    # Run the form filler
    result = await analyze_and_fill_form(form_url, user_uuid, reg_id)
    
    if not result['success']:
        await query.edit_message_text(f"❌ Auto-fill failed: {result['error']}")
        return
    
    # Show summary to user
    filled_fields = result['filled_fields']
    unmatched = result.get('unmatched_fields', [])
    
    msg = "✅ <b>Form Auto-Filled!</b>\n\n"
    msg += "<b>Fields I filled:</b>\n"
    for label, value in filled_fields.items():
        msg += f"• <b>{label}:</b> {value}\n"
    
    if unmatched:
        msg += f"\n⚠️ <b>Fields I couldn't fill:</b>\n"
        for field in unmatched:
            msg += f"• {field}\n"
    
    msg += "\n<b>What would you like to do?</b>"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Proceed & Submit", callback_data=f"reg_proceed_{reg_id}"),
            InlineKeyboardButton("✏️ Edit Fields", callback_data=f"reg_edit_{reg_id}")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"reg_cancel_{reg_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Store reg_id for editing
    context.user_data['awaiting_reg_edit'] = reg_id
    
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=reply_markup)

async def handle_registration_proceed(query, reg_id: str):
    """Submits the form."""
    await query.edit_message_text("📤 <b>Submitting form...</b>", parse_mode='HTML')
    
    result = await submit_form(reg_id)
    
    if result['success']:
        await query.edit_message_text("✅ <b>Form submitted successfully!</b>\n\nYou'll receive a confirmation email shortly.", parse_mode='HTML')
    else:
        await query.edit_message_text(f"❌ Submission failed: {result['error']}")

async def handle_registration_edit(query, context, reg_id: str):
    """Asks user which field to edit."""
    context.user_data['awaiting_reg_edit'] = reg_id
    context.user_data['awaiting_reg_edit_mode'] = True
    
    await query.edit_message_text(
        "✏️ <b>Edit Mode</b>\n\n"
        "Reply with the field name and new value.\n"
        "Example: <code>Phone: 9876543210</code>",
        parse_mode='HTML'
    )

async def handle_registration_cancel(query, reg_id: str):
    """Cancels the form filling."""
    await cancel_form(reg_id)
    await query.edit_message_text("❌ Form filling cancelled.")

async def handle_registration_to_task(query, reg_id: str):
    """Saves the registration as a Google Task."""
    reg_data = supabase_client.table('registrations').select('form_url, form_title, user_id').eq('id', reg_id).execute()
    if not reg_data.data:
        await query.edit_message_text("❌ Registration not found.")
        return
    
    reg = reg_data.data[0]
    from src.tools.google_auth import get_valid_credentials
    from src.tools.tasks_api import create_task
    
    creds = get_valid_credentials(reg['user_id'])
    if not creds:
        await query.edit_message_text("❌ Google session expired.")
        return
    
    task_title = f"Register for: {reg['form_title'] or 'Form'}"
    task_notes = f"Link: {reg['form_url']}"
    
    task_id = create_task(creds, task_title, task_notes)
    if task_id:
        supabase_client.table('registrations').update({'status': 'Saved to Tasks'}).eq('id', reg_id).execute()
        await query.edit_message_text(f"✅ Saved to Google Tasks!\n📌 {task_title}")
    else:
        await query.edit_message_text("❌ Failed to create task.")