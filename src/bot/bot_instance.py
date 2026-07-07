from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.request import HTTPXRequest
from src.core.config import settings
from src.bot.handlers.commands import start_command, recent_command, tasks_command, calendar_command, search_command, draft_command, briefing_command, expenses_command, profile_command, set_github_token_command
from src.bot.handlers.callbacks import button_callback
from src.bot.handlers.messages import handle_message
from src.core.logging import logger
from src.bot.handlers.voice_handler import handle_voice_message

# 🔥 NEW: Create a custom request object with higher timeouts
request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=30.0,
    pool_timeout=30.0
)

# Create the Telegram bot application with the custom request
application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).request(request).build()

# Register command handlers
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("recent", recent_command))
application.add_handler(CommandHandler("tasks", tasks_command))
application.add_handler(CommandHandler("calendar", calendar_command))
application.add_handler(CommandHandler("search", search_command))
application.add_handler(CommandHandler("draft", draft_command))
application.add_handler(CommandHandler("briefing", briefing_command))
application.add_handler(CommandHandler("sync", sync_command))
application.add_handler(CommandHandler("expenses", expenses_command))
application.add_handler(CommandHandler("profile", profile_command))
application.add_handler(CommandHandler("set_github_token", set_github_token_command))

# Register callback handler for inline buttons
application.add_handler(CallbackQueryHandler(button_callback))

# Register message handler for free-text chat (routes to RAG)
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))

logger.info("✅ Telegram bot handlers registered.")