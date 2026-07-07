import os
import tempfile
from telegram import Update
from telegram.ext import ContextTypes
from groq import Groq # pip install groq
from src.core.config import settings
from src.core.logging import logger
from src.services.draft_service import generate_email_draft
from src.tools.tasks_api import create_task
from src.tools.google_auth import get_valid_credentials
from src.db.client import get_user_uuid_by_telegram

groq_client = Groq(api_key=settings.GROQ_API_KEY)

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles voice notes: Transcribes -> Analyzes Intent -> Executes."""
    telegram_id = update.effective_user.id
    user_uuid = get_user_uuid_by_telegram(telegram_id)
    
    if not user_uuid:
        await update.message.reply_text("❌ Please use /start first.")
        return

    await update.message.reply_text("🎙️ Transcribing voice note...")
    
    try:
        # 1. Download voice file
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            await voice_file.download_to_drive(temp_file.name)
            temp_path = temp_file.name

        # 2. Transcribe with Groq Whisper
        with open(temp_path, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(temp_path, file.read()),
                model="whisper-large-v3",
                language="en",
                response_format="text"
            )
        
        os.unlink(temp_path) # Clean up
        logger.info(f"Transcribed: {transcription}")
        
        await update.message.reply_text(f"📝 Transcribed: \"{transcription}\"\n\n🤔 Analyzing intent...")

        # 3. Analyze Intent (Simple keyword routing for now, can use LLM later)
        text_lower = transcription.lower()
        
        if "email" in text_lower or "send" in text_lower or "draft" in text_lower:
            # Route to Draft Service
            draft = generate_email_draft(transcription, user_uuid)
            if draft:
                await update.message.reply_text(f"✍️ Draft created!\n\nTo: {draft['to']}\nSubject: {draft['subject']}\n\n{draft['body']}")
            else:
                await update.message.reply_text("❌ Failed to generate draft.")
                
        elif "task" in text_lower or "remind" in text_lower or "todo" in text_lower:
            # Route to Task Service
            creds = get_valid_credentials(user_uuid)
            if creds:
                task_id = create_task(creds, transcription)
                await update.message.reply_text(f"✅ Task created: {transcription}")
            else:
                await update.message.reply_text("❌ Google session expired.")
                
        else:
            # Default to RAG Search
            from src.services.rag_service import chat_with_emails
            answer = await chat_with_emails(user_id=user_uuid, question=transcription)
            await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Voice processing failed: {e}")
        await update.message.reply_text("❌ Failed to process voice note.")