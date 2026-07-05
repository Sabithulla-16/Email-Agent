from telegram import Update
from telegram.ext import ContextTypes
from src.core.logging import logger

async def send_formatted_message(update: Update, text: str):
    """
    Safely sends a message using HTML parse mode.
    Falls back to plain text if the LLM outputs invalid HTML.
    """
    try:
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"HTML parsing failed, falling back to plain text: {e}")
        # Strip HTML tags for the fallback plain text
        clean_text = text.replace('<b>', '').replace('</b>', '') \
                         .replace('<i>', '').replace('</i>', '') \
                         .replace('<ul>', '').replace('</ul>', '') \
                         .replace('<ol>', '').replace('</ol>', '') \
                         .replace('<li>', '- ').replace('</li>', '') \
                         .replace('<p>', '').replace('</p>', '\n') \
                         .replace('<br>', '\n').replace('<br/>', '\n')
        await update.message.reply_text(clean_text)

async def send_html_to_chat(chat_id: int, text: str, bot):
    """
    Safely sends a message to a specific chat ID (used for background tasks).
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"HTML parsing failed in background task, falling back to plain text: {e}")
        clean_text = text.replace('<b>', '').replace('</b>', '') \
                         .replace('<i>', '').replace('</i>', '') \
                         .replace('<ul>', '').replace('</ul>', '') \
                         .replace('<ol>', '').replace('</ol>', '') \
                         .replace('<li>', '- ').replace('</li>', '') \
                         .replace('<p>', '').replace('</p>', '\n') \
                         .replace('<br>', '\n').replace('<br/>', '\n')
        await bot.send_message(chat_id=chat_id, text=clean_text)