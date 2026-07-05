import sys
import os
import asyncio

# Add the root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the bot instance so it's initialized
from src.bot.bot_instance import application 
from src.services.scheduler import send_morning_briefings
from src.core.logging import logger

async def run_test():
    logger.info("🚀 Initializing bot for scheduler test...")
    await application.initialize()
    await application.start()
    
    logger.info("🧪 Manually triggering morning briefing...")
    await send_morning_briefings()
    
    logger.info("✅ Test complete. Shutting down...")
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    asyncio.run(run_test())