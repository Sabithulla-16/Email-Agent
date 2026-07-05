from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.bot.bot_instance import application as telegram_bot
from src.api.telegram_webhook import router as telegram_router
from src.api.gmail_webhook import router as gmail_router
from src.api.auth_router import router as auth_router
from src.core.logging import logger

# 🔥 IMPORT THE SCHEDULER FUNCTIONS
from src.services.scheduler import start_scheduler, shutdown_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # --- STARTUP ---
    logger.info("🚀 Starting Email Agent Backend...")
    await telegram_bot.initialize()
    await telegram_bot.start()
    logger.info("✅ Telegram bot started successfully!")
    
    # 🔥 START THE SCHEDULER
    start_scheduler()
    
    yield
    
    # --- SHUTDOWN ---
    logger.info("🛑 Shutting down...")
    
    # 🔥 STOP THE SCHEDULER
    shutdown_scheduler()
    
    await telegram_bot.stop()
    await telegram_bot.shutdown()

app = FastAPI(title="Email Agent API", lifespan=lifespan)

# Include routers
app.include_router(telegram_router, prefix="/webhooks")
app.include_router(gmail_router, prefix="/webhooks")
app.include_router(auth_router)

@app.get("/")
async def root():
    return {"message": "Email Agent is running!"}