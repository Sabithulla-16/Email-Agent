import html
import asyncio
from telegram.error import BadRequest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram, supabase_client
from src.tools.google_auth import get_valid_credentials
from src.tools.gmail_api import send_email
from src.services.quick_reply_service import pending_quick_replies, generate_quick_reply_email
from src.core.logging import logger

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
    query = update.callback_query
    
    # 🔥 FIX: Wrap query.answer() in try-except to handle timeouts gracefully
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"⚠️ Failed to answer callback query (timeout): {e}")
        
    callback_data = query.data
    telegram_id = int(query.from_user.id)
    logger.info(f"Button clicked: {callback_data}")
    
    # 🔥 HANDLE REGISTRATION BUTTONS (Auto-Fill)
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
        await handle_registration_proceed(query, context, reg_id)
        return
    if callback_data.startswith("reg_cancel_"):
        reg_id = callback_data.split("_")[2]
        from src.services.form_filler_service import cancel_form
        await cancel_form(reg_id)
        await query.edit_message_text(" Form filling cancelled.")
        return

    # 🔥 HANDLE QUICK REPLY BUTTONS
    if callback_data.startswith("quick_"):
        await handle_quick_reply_click(query, callback_data, context)  # 🔥 Pass context!
        return

    # HANDLE DRAFT BUTTONS
    if callback_data == "approve_draft":
        draft = context.user_data.get('pending_draft')
        if not draft:
            await query.edit_message_text("❌ Draft expired or not found.")
            return
        await query.edit_message_text(" Sending email...")
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
            "Please tell me what you'd like to change.\n\n"
            "Examples:\n"
            "- 'Change the signature to John Doe'\n"
            "- 'Make the tone more formal'",
            parse_mode='HTML'
        )
    elif callback_data == "cancel_draft":
        context.user_data.pop('pending_draft', None)
        await query.edit_message_text("❌ Email draft cancelled.")

async def handle_quick_reply_click(query, callback_data: str, context: ContextTypes.DEFAULT_TYPE):
    """Handles quick reply button clicks."""
    parts = callback_data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("❌ Invalid quick reply.")
        return
    
    option_index = int(parts[-1])
    reply_id = "_".join(parts[1:-1])
    
    quick_reply = pending_quick_replies.get(reply_id)
    if not quick_reply:
        await query.edit_message_text("❌ This quick reply has expired or was already used.")
        return
    
    if option_index >= len(quick_reply['options']):
        await query.edit_message_text("❌ Invalid option.")
        return
    
    selected_option = quick_reply['options'][option_index]
    await query.edit_message_text(f"✍️ Generating reply: '{selected_option['label']}'...")
    
    # 🔥 CRITICAL: Use await and pass the snippet for GitHub context
    reply_email = await generate_quick_reply_email(
        sender=quick_reply['sender'],
        subject=quick_reply['subject'],
        intent=selected_option['intent'],
        original_email_snippet=quick_reply.get('snippet', ''),  # 🔥 Pass snippet!
        user_uuid=quick_reply['user_uuid']
    )
    
    if not reply_email:
        await query.edit_message_text("❌ Failed to generate reply. Please try again.")
        return
    
    # 🔥 FIX: Save to pending_draft and show approval buttons
    context.user_data['pending_draft'] = reply_email
    context.user_data['pending_draft_user_uuid'] = quick_reply['user_uuid']
    
    # Format the draft message
    msg = f"📝 <b>Here is the quick reply draft:</b>\n\n"
    msg += f"👤 To: {quick_reply['sender']}\n"
    msg += f"📌 Subject: {reply_email['subject']}\n\n"
    msg += f"{reply_email['body']}\n\n"
    msg += f"👇 What would you like to do?"
    
    # Create Inline Buttons for Approval
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve & Send", callback_data="approve_draft"),
            InlineKeyboardButton("✏️ Edit Draft", callback_data="edit_draft"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_draft")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='HTML')

# 🔥 FIXED: Auto-Fill Registration Handlers
async def handle_registration_autofill(query, context, reg_id: str):
    """Starts the auto-fill process in the background to prevent timeout."""
    try:
        await query.edit_message_text(
            "🤖 <b>Opening browser and analyzing form...</b>\n\n"
            "<i>This may take 10-20 seconds on the first run.</i>", 
            parse_mode='HTML'
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    
    reg_data = supabase_client.table('registrations').select('*').eq('id', reg_id).execute()
    if not reg_data.data:
        await query.edit_message_text("❌ Registration not found.")
        return
    
    # 🔥 CRITICAL FIX: Run in background to prevent timeout!
    asyncio.create_task(
        run_autofill_in_background(
            context, 
            query.message.chat_id, 
            query.message.message_id, 
            reg_id
        )
    )

async def run_autofill_in_background(context, chat_id: int, message_id: int, reg_id: str):
    """Runs the browser agent in the background and updates the message when done."""
    from src.services.form_filler_service import analyze_and_fill_form
    from src.db.client import supabase_client

    reg_data = supabase_client.table('registrations').select('*').eq('id', reg_id).execute()
    if not reg_data.data:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="❌ Registration not found.")
        return
    
    reg = reg_data.data[0]
    user_uuid = reg['user_id']
    form_url = reg['form_url']
    
    # Run the form filler
    result = await analyze_and_fill_form(form_url, user_uuid, reg_id)
    
    if not result['success']:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=f"❌ <b>Auto-fill failed:</b>\n{result['error']}", 
                parse_mode='HTML'
            )
        except:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Auto-fill failed: {result['error']}")
        return
    
    # Build detailed summary
    filled_fields = result['filled_fields']
    all_fields = result.get('all_fields', [])
    unmatched = result.get('unmatched_fields', [])
    
    msg = "📋 <b>Form Analysis Complete!</b>\n\n"
    
    # Show all detected fields
    if all_fields:
        msg += "<b>🔍 Detected Fields:</b>\n"
        for field in all_fields:
            label = field.get('label', 'Unknown')
            filled_value = filled_fields.get(label)
            
            if filled_value:
                msg += f"✅ <b>{label}:</b> {filled_value}\n"
            else:
                msg += f"⚠️ <b>{label}:</b> <i>Not filled</i>\n"
        msg += "\n"
    
    # Show unmatched fields that need user input
    if unmatched:
        msg += "❓ <b>Need Your Input:</b>\n"
        for field_label in unmatched:
            msg += f"• {field_label}\n"
        
        msg += "\n<b>Please provide these details in this format:</b>\n"
        msg += "<code>Field Name: Your Answer</code>\n\n"
        msg += "<i>Example: Team Name: Byte Builders</i>"
        
        # Set state to await user input
        context.user_data['awaiting_form_fields'] = reg_id
        context.user_data['awaiting_form_unmatched'] = unmatched
    else:
        msg += "✅ <b>All fields filled successfully!</b>\n\n"
        msg += "<b>Ready to submit?</b>"
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [
                InlineKeyboardButton("✅ Proceed & Submit", callback_data=f"reg_proceed_{reg_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"reg_cancel_{reg_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=msg, parse_mode='HTML', reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Could not edit message: {e}")
            await context.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode='HTML', reply_markup=reply_markup
            )
        return
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=msg, parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"Could not edit message: {e}")
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')

async def handle_registration_proceed(query, context, reg_id: str):
    """Submits the form in the background."""
    await query.edit_message_text("📤 <b>Submitting form...</b>", parse_mode='HTML')
    
    # Run submission in background
    asyncio.create_task(run_submission_in_background(context, query.message.chat_id, query.message.message_id, reg_id))

async def run_submission_in_background(context, chat_id: int, message_id: int, reg_id: str):
    """Runs form submission in the background."""
    from src.services.form_filler_service import submit_form
    
    result = await submit_form(reg_id)
    
    try:
        if result['success']:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text="✅ <b>Form submitted successfully!</b>\n\nYou'll receive a confirmation email shortly.", 
                parse_mode='HTML'
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=f"❌ Submission failed: {result['error']}", 
                parse_mode='HTML'
            )
    except Exception as e:
        logger.warning(f"Could not update submission message: {e}")
        await context.bot.send_message(
            chat_id=chat_id, 
            text="✅ Form submitted successfully!" if result['success'] else f"❌ Submission failed: {result['error']}"
        )

async def handle_registration_to_task(query, reg_id: str):
    """Saves the registration as a Google Task."""
    reg_data = supabase_client.table('registrations').select('form_url, form_title, user_id').eq('id', reg_id).execute()
    if not reg_data.data:
        await query.edit_message_text("❌ Registration not found.")
        return
    
    reg = reg_data.data[0]
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