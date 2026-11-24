"""API v1 router registry."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.cron import router as cron_router
from app.api.v1.drafts import router as drafts_router
from app.api.v1.emails import router as emails_router
from app.api.v1.features import router as features_router
from app.api.v1.health import router as health_router
from app.api.v1.telegram import router as telegram_router

api_router = APIRouter(prefix="/api/v1")

# Core Health probes
api_router.include_router(health_router)

# Authentication & User Profile
api_router.include_router(auth_router)

# Emails & Sync
api_router.include_router(emails_router)

# Drafts & Approvals
api_router.include_router(drafts_router)

# Telegram Bot & Webhooks
api_router.include_router(telegram_router)

# Feature-Flagged Extensions
api_router.include_router(features_router)

# Free Tier & Serverless Cron Scheduling
api_router.include_router(cron_router)
