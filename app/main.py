"""FastAPI Application Entrypoint."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import sentry_sdk
from fastapi import FastAPI, Response, status

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings, validate_startup_config
from app.core.logging import get_logger, setup_logging
from app.core.middleware import register_exception_handlers, setup_middleware
from app.db.client import check_supabase_health, close_supabase, init_supabase
from app.queue.redis import check_redis_health, close_redis, init_redis
from app.services.scheduler_runner import run_direct_sync_cycle
from app.services.telegram_client import TelegramClient


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifespan management."""
    settings = get_settings()
    log_lvl = "DEBUG" if settings.DEBUG else "INFO"
    setup_logging(is_production=settings.is_production, log_level=log_lvl)
    logger = get_logger("app.main")

    # Fail-fast validation of required environment variables
    validate_startup_config(settings)

    logger.info("app_starting", env=settings.ENVIRONMENT, host=settings.HOST, port=settings.PORT)

    # Optional Sentry initialization
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN.get_secret_value(),
            environment=settings.ENVIRONMENT,
            traces_sample_rate=0.1 if settings.is_production else 1.0,
        )
        logger.info("sentry_initialized")

    # Connect to infrastructure dependencies
    try:
        await init_supabase(settings)
    except Exception as exc:
        logger.error("supabase_init_error", error=str(exc))

    try:
        await init_redis(settings)
    except Exception as exc:
        logger.error("redis_init_error", error=str(exc))

    # Configure Telegram webhook if in webhook mode
    if settings.TELEGRAM_MODE == "webhook":
        try:
            tg_client = TelegramClient(settings=settings)
            webhook_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/telegram/webhook"
            secret_token = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
            success = await tg_client.set_webhook(url=webhook_url, secret_token=secret_token)
            if success:
                logger.info("telegram_webhook_configured", url=webhook_url)
            else:
                logger.error("telegram_webhook_setup_failed", url=webhook_url)

            info = await tg_client.get_webhook_info()
            if info:
                logger.info(
                    "telegram_webhook_info",
                    url=info.get("url"),
                    pending_update_count=info.get("pending_update_count"),
                    last_error_message=info.get("last_error_message"),
                )
        except Exception as exc:
            logger.error("telegram_webhook_startup_error", error=str(exc))

    # Free-tier In-Process Scheduler (runs periodic inbox sync inside Web Service)
    sync_task: asyncio.Task[None] | None = None
    if settings.ENABLE_IN_PROCESS_SCHEDULER and settings.ENVIRONMENT != "test":
        async def _background_scheduler_loop() -> None:
            await asyncio.sleep(15)  # initial warmup delay
            while True:
                try:
                    await run_direct_sync_cycle()
                except asyncio.CancelledError:
                    break
                except Exception as loop_exc:
                    logger.error("in_process_scheduler_loop_error", error=str(loop_exc))
                await asyncio.sleep(settings.IN_PROCESS_SYNC_INTERVAL_SECONDS)

        sync_task = asyncio.create_task(_background_scheduler_loop())
        logger.info(
            "in_process_scheduler_started",
            interval_seconds=settings.IN_PROCESS_SYNC_INTERVAL_SECONDS,
        )

    yield

    if sync_task:
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task

    logger.info("app_shutting_down")
    await close_redis()
    await close_supabase()
    logger.info("app_shutdown_complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI application factory."""
    cfg = settings or get_settings()

    app = FastAPI(
        title="Email Agent API",
        description="Production-grade AI email agent backend with Groq, Gmail, and Telegram.",
        version="1.0.0",
        docs_url="/docs" if not cfg.is_production else None,
        redoc_url="/redoc" if not cfg.is_production else None,
        openapi_url="/openapi.json" if not cfg.is_production else None,
        lifespan=lifespan,
        swagger_ui_parameters={"withCredentials": True, "persistAuthorization": True},
    )

    setup_middleware(app, cfg)
    register_exception_handlers(app)

    # Top-level health endpoints for ingress / load-balancers
    @app.get("/healthz", tags=["Health"], status_code=status.HTTP_200_OK)
    async def root_healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "telegram": {
                "mode": cfg.TELEGRAM_MODE,
                "bot_username": f"@{cfg.TELEGRAM_BOT_USERNAME.lstrip('@')}",
                "status": "configured" if cfg.TELEGRAM_BOT_TOKEN else "disabled",
            },
        }

    @app.get("/readyz", tags=["Health"])
    async def root_readyz(response: Response) -> dict[str, object]:
        db_ok, db_detail = await check_supabase_health()
        redis_ok, redis_detail = await check_redis_health()
        is_ready = db_ok and redis_ok
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if is_ready else "degraded",
            "services": {
                "database": {"healthy": db_ok, "detail": db_detail},
                "redis": {"healthy": redis_ok, "detail": redis_detail},
            },
        }

    # API v1 routes
    app.include_router(api_router)

    return app


app = create_app()
