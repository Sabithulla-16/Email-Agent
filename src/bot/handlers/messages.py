from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram
from src.services.rag_service import chat_with_emails
from src.services.draft_service import generate_email_draft
from src.core.logging import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.bot.utils import send_formatted_message
from src.services.ingestion import update_crm_and_preferences
from src.services.form_filler_service import edit_field

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles free-text messages - routes to RAG chat, Draft generation, or Draft editing."""
    user_message = update.message.text
    telegram_id = int(update.effective_user.id)
    
    # 🌟 CHECK IF WE ARE IN DRAFT EDIT MODE
    if context.user_data.get('awaiting_draft_edit'):
        await handle_draft_edit(update, context, user_message)
        return
    
    # CHECK IF WE ARE IN INITIAL DRAFT MODE
    if context.user_data.get('awaiting_draft_intent'):
        await handle_draft_intent(update, context, user_message)
        return

    # CHECK IF WE ARE EDITING A FORM FIELD
    if context.user_data.get('awaiting_reg_edit_mode'):
        await handle_form_field_edit(update, context, user_message)
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
    context.user_data['pending_draft_user_uuid'] = user_uuid  # ✅ Store for edit function
    
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
    
    # 🔥 ADD THIS: Capture the edit instruction for continuous learning
    telegram_id = int(update.effective_user.id)
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    
    # 🔥 Feed the edit instruction back into the learning loop
    update_crm_and_preferences(user_uuid, "Self", "Neutral", edit_instruction=edit_instruction)
    
    # Clear the edit flag
    context.user_data['awaiting_draft_edit'] = False
    
    # Get the current draft
    current_draft = context.user_data.get('pending_draft')
    user_uuid = context.user_data.get('pending_draft_user_uuid')  # ✅ Get stored user_uuid
    
    if not current_draft:
        await update.message.reply_text("❌ Draft not found. Please start over with /draft")
        return
    
    # ✅ Pass user_uuid to the function
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

async def handle_form_field_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """Handles editing a form field."""
    reg_id = context.user_data.get('awaiting_reg_edit')
    
    # Parse the message (format: "Field Name: New Value")
    if ':' not in user_message:
        await update.message.reply_text("❌ Invalid format. Use: <code>Field Name: New Value</code>", parse_mode='HTML')
        return
    
    parts = user_message.split(':', 1)
    field_label = parts[0].strip()
    new_value = parts[1].strip()
    
    result = await edit_field(reg_id, field_label, new_value)
    
    if result['success']:
        # Show updated summary
        filled_fields = result['filled_fields']
        msg = "✅ <b>Field Updated!</b>\n\n"
        msg += "<b>Current fields:</b>\n"
        for label, value in filled_fields.items():
            msg += f"• <b>{label}:</b> {value}\n"
        
        msg += "\n<b>Ready to submit?</b>"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Proceed & Submit", callback_data=f"reg_proceed_{reg_id}"),
                InlineKeyboardButton("✏️ Edit More", callback_data=f"reg_edit_{reg_id}")
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data=f"reg_cancel_{reg_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update.message.reply_text(f"❌ Edit failed: {result['error']}")