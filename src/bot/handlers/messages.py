from telegram import Update
from telegram.ext import ContextTypes
from src.db.client import get_user_uuid_by_telegram
from src.services.rag_service import chat_with_emails
from src.services.draft_service import generate_email_draft
from src.core.logging import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.bot.utils import send_formatted_message

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
    
    # Generate draft using Groq
    draft_data = generate_email_draft(intent)
    
    if not draft_data:
        await update.message.reply_text("❌ Sorry, I couldn't generate a draft. Please try again.")
        return
        
    # Save draft to user_data for the callback buttons
    context.user_data['pending_draft'] = draft_data
    
    # Format the message (Using plain text to avoid Markdown parsing errors with LLM output)
    msg = f"📝 *Here is the draft:*\n\n"
    msg += f"👤 To: {draft_data['to']}\n"
    msg += f"📌 Subject: {draft_data['subject']}\n\n"
    msg += f"{draft_data['body']}\n\n"
    msg += f"👇 What would you like to do?"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve & Send", callback_data="approve_draft"),
            InlineKeyboardButton("✏️ Edit Draft", callback_data="edit_draft"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_draft")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(msg, reply_markup=reply_markup)

async def handle_draft_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_instruction: str):
    """Regenerates the draft based on user's edit instructions."""
    await update.message.reply_text("✏️ Updating your draft...")
    
    # Clear the edit flag
    context.user_data['awaiting_draft_edit'] = False
    
    # Get the current draft
    current_draft = context.user_data.get('pending_draft')
    if not current_draft:
        await update.message.reply_text("❌ Draft not found. Please start over with /draft")
        return
    
    # Use Groq to apply the edit
    from src.services.draft_service import regenerate_draft_with_edit
    updated_draft = regenerate_draft_with_edit(current_draft, edit_instruction)
    
    if not updated_draft:
        await update.message.reply_text("❌ Sorry, I couldn't update the draft. Please try again.")
        return
    
    # Save the updated draft
    context.user_data['pending_draft'] = updated_draft
    
    # Format the updated message
    msg = f"📝 *Updated Draft:*\n\n"
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
    
    await update.message.reply_text(msg, reply_markup=reply_markup)