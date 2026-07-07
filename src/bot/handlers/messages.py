from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram
from src.services.rag_service import chat_with_emails
from src.services.draft_service import generate_email_draft
from src.core.logging import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.bot.utils import send_formatted_message
from src.services.ingestion import update_crm_and_preferences

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles free-text messages - routes to RAG chat, Draft generation, or Form field input."""
    user_message = update.message.text
    telegram_id = int(update.effective_user.id)
    
    # 🌟 CHECK IF WE ARE IN DRAFT EDIT MODE
    if context.user_data.get('awaiting_draft_edit'):
        await handle_draft_edit(update, context, user_message)
        return

    # 🔥 CHECK IF WE ARE COLLECTING FORM FIELDS
    if context.user_data.get('awaiting_form_fields'):
        await handle_form_field_input(update, context, user_message)
        return
    
    # CHECK IF WE ARE IN INITIAL DRAFT MODE
    if context.user_data.get('awaiting_draft_intent'):
        await handle_draft_intent(update, context, user_message)
        return

    # Otherwise, it's a RAG query
    logger.info(f"User {telegram_id} sent RAG message: {user_message}")
    await update.message.reply_text("🤔 Searching your emails...")
    
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    if not user_uuid:
        await update.message.reply_text("❌ You are not logged in. Please use /start first.")
        return
        
    answer = await chat_with_emails(user_id=user_uuid, question=user_message)
    await send_formatted_message(update, answer)

async def handle_draft_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: str):
    """Generates the draft based on the user's intent."""
    await update.message.reply_text("✍️ Drafting your email...")
    
    # Clear the flag
    context.user_data['awaiting_draft_intent'] = False
    
    # ✅ Get user_uuid
    telegram_id = int(update.effective_user.id)
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    
    # ✅ Pass user_uuid to the function
    draft_data = generate_email_draft(intent, user_uuid=user_uuid)
    
    if not draft_data:
        await update.message.reply_text("❌ Sorry, I couldn't generate a draft. Please try again.")
        return
        
    # Save draft to user_data for the callback buttons
    context.user_data['pending_draft'] = draft_data
    context.user_data['pending_draft_user_uuid'] = user_uuid
    
    # Format the message
    msg = f"📝 <b>Here is the draft:</b>\n\n"
    msg += f"👤 To: {draft_data['to']}\n"
    msg += f"📌 Subject: {draft_data['subject']}\n\n"
    msg += f"{draft_data['body']}\n\n"
    msg += f"👇 What would you like to do?"
    
    # Create Inline Buttons
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve & Send", callback_data="approve_draft"),
            InlineKeyboardButton("✏️ Edit Draft", callback_data="edit_draft"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_draft")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='HTML')

async def handle_draft_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_instruction: str):
    """Regenerates the draft based on user's edit instructions."""
    await update.message.reply_text("✏️ Updating your draft...")
    
    telegram_id = int(update.effective_user.id)
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    
    # Feed the edit instruction back into the learning loop
    update_crm_and_preferences(user_uuid, "Self", "Neutral", edit_instruction=edit_instruction)
    
    # Clear the edit flag
    context.user_data['awaiting_draft_edit'] = False
    
    # Get the current draft
    current_draft = context.user_data.get('pending_draft')
    user_uuid = context.user_data.get('pending_draft_user_uuid')
    
    if not current_draft:
        await update.message.reply_text("❌ Draft not found. Please start over with /draft")
        return
    
    # Pass user_uuid to the function
    from src.services.draft_service import regenerate_draft_with_edit
    updated_draft = regenerate_draft_with_edit(current_draft, edit_instruction, user_uuid=user_uuid)
    
    if not updated_draft:
        await update.message.reply_text("❌ Sorry, I couldn't update the draft. Please try again.")
        return
    
    # Save the updated draft
    context.user_data['pending_draft'] = updated_draft
    
    # Format the updated message
    msg = f"📝 <b>Updated Draft:</b>\n\n"
    msg += f"👤 To: {updated_draft['to']}\n"
    msg += f"📌 Subject: {updated_draft['subject']}\n\n"
    msg += f"{updated_draft['body']}\n\n"
    msg += f"👇 What would you like to do?"
    
    # Show the buttons again
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve & Send", callback_data="approve_draft"),
            InlineKeyboardButton("✏️ Edit Draft", callback_data="edit_draft"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_draft")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='HTML')

async def handle_form_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """Handles user input for missing form fields."""
    reg_id = context.user_data.get('awaiting_form_fields')
    unmatched_fields = context.user_data.get('awaiting_form_unmatched', [])
    
    await update.message.reply_text("📝 Processing your input...")
    
    # Parse user input (format: "Field Name: Value" or "Field1: Value1, Field2: Value2")
    user_responses = {}
    
    # Check if multiple fields are provided
    if ',' in user_message and ':' in user_message:
        # Multiple fields format
        parts = user_message.split(',')
        for part in parts:
            if ':' in part:
                field_label, value = part.split(':', 1)
                user_responses[field_label.strip()] = value.strip()
    elif ':' in user_message:
        # Single field format
        field_label, value = user_message.split(':', 1)
        user_responses[field_label.strip()] = value.strip()
    else:
        # If user just provided a value, assume it's for the first unmatched field
        if len(unmatched_fields) == 1:
            user_responses[unmatched_fields[0]] = user_message.strip()
        else:
            await update.message.reply_text(
                "❌ Please use the format: <code>Field Name: Your Answer</code>\n\n"
                f"Fields needed: {', '.join(unmatched_fields)}",
                parse_mode='HTML'
            )
            return
    
    # Fill the additional fields
    from src.services.form_filler_service import fill_additional_fields
    result = await fill_additional_fields(reg_id, user_responses)
    
    if not result['success']:
        await update.message.reply_text(f"❌ Failed to process: {result['error']}")
        return
    
    # Check if all fields are now filled
    remaining_unmatched = result.get('unmatched_fields', [])
    
    if remaining_unmatched:
        # Still have missing fields
        msg = "✅ <b>Fields updated!</b>\n\n"
        msg += "<b>Current filled fields:</b>\n"
        for label, value in result['filled_fields'].items():
            msg += f"• <b>{label}:</b> {value}\n"
        
        msg += f"\n⚠️ <b>Still need:</b>\n"
        for field in remaining_unmatched:
            msg += f"• {field}\n"
        
        msg += "\nPlease provide the remaining details."
        
        context.user_data['awaiting_form_unmatched'] = remaining_unmatched
        await update.message.reply_text(msg, parse_mode='HTML')
    else:
        # All fields filled! Show final summary
        msg = "✅ <b>All fields filled successfully!</b>\n\n"
        msg += "<b>Complete form data:</b>\n"
        for label, value in result['filled_fields'].items():
            msg += f"• <b>{label}:</b> {value}\n"
        
        msg += "\n<b>Ready to submit?</b>"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Proceed & Submit", callback_data=f"reg_proceed_{reg_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"reg_cancel_{reg_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Clear the awaiting state
        context.user_data.pop('awaiting_form_fields', None)
        context.user_data.pop('awaiting_form_unmatched', None)
        
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)