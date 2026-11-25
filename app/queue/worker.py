"""ARQ background worker for scheduled email sync, Groq analysis, drafting, and digests."""

import inspect
from datetime import UTC, datetime
from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings, validate_startup_config
from app.db.client import close_supabase, get_supabase, init_supabase
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserUpdate
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.digest_service import (
    DEFAULT_DIGEST_SLOTS,
    DigestService,
    compute_next_digest_time,
)
from app.services.groq_service import GroqService
from app.services.notification_service import NotificationService
from app.services.scheduled_email_service import ScheduledEmailService
from app.services.sync_service import SyncService

logger = structlog.get_logger("app.queue.worker")


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize Supabase client, repositories, and domain services in ARQ context."""
    settings = get_settings()
    validate_startup_config(settings)
    ctx["settings"] = settings

    await init_supabase(settings)
    client = get_supabase()

    user_repo = UserRepo(client)
    email_repo = EmailRepo(client)
    draft_repo = DraftRepo(client)
    telegram_repo = TelegramRepo(client)
    rule_repo = RuleRepo(client)

    auth_svc = AuthService(user_repo=user_repo, settings=settings)
    sync_svc = SyncService(
        user_repo=user_repo,
        email_repo=email_repo,
        auth_service=auth_svc,
        settings=settings,
    )
    groq_svc = GroqService(settings=settings)
    approval_svc = ApprovalService(
        draft_repo=draft_repo,
        email_repo=email_repo,
        user_repo=user_repo,
        auth_service=auth_svc,
        settings=settings,
    )
    notif_svc = NotificationService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        settings=settings,
    )
    digest_svc = DigestService(
        user_repo=user_repo,
        email_repo=email_repo,
        draft_repo=draft_repo,
        telegram_repo=telegram_repo,
        auth_service=auth_svc,
        settings=settings,
    )

    ctx["user_repo"] = user_repo
    ctx["email_repo"] = email_repo
    ctx["draft_repo"] = draft_repo
    ctx["telegram_repo"] = telegram_repo
    ctx["rule_repo"] = rule_repo
    ctx["sync_service"] = sync_svc
    ctx["groq_service"] = groq_svc
    ctx["approval_service"] = approval_svc
    ctx["notification_service"] = notif_svc
    ctx["digest_service"] = digest_svc

    logger.info("arq_worker_startup_completed")


async def shutdown(_ctx: dict[str, Any]) -> None:
    """Gracefully terminate client connection pools."""
    await close_supabase()
    logger.info("arq_worker_shutdown_completed")


# -----------------------------------------------------------------------------
# Background Jobs
# -----------------------------------------------------------------------------
async def run_user_sync_pipeline(ctx: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Execute end-to-end user sync pipeline: sync -> analyze -> draft -> notify."""
    sync_svc: SyncService = ctx["sync_service"]
    email_repo: EmailRepo = ctx["email_repo"]
    groq_svc: GroqService = ctx["groq_service"]
    approval_svc: ApprovalService = ctx["approval_service"]
    notif_svc: NotificationService = ctx["notification_service"]
    draft_repo: DraftRepo = ctx["draft_repo"]

    logger.info("starting_user_sync_pipeline", user_id=user_id)

    # 1. Sync emails from Gmail
    sync_res = await sync_svc.sync_user_emails(user_id)
    new_count = sync_res.get("synced", sync_res.get("new_emails_synced", 0))

    # Require user's personal Groq API key (BYOK) before running sync/LLM pipeline
    user_repo: Any = ctx.get("user_repo")
    user_groq_key: str | None = None
    auth_svc: Any = getattr(approval_svc, "auth_service", None) or ctx.get("auth_service")
    if user_repo and inspect.iscoroutinefunction(getattr(user_repo, "get_by_id", None)):
        u_row = await user_repo.get_by_id(user_id)
        if u_row and not (u_row.prefs and u_row.prefs.get("groq_api_key_encrypted")):
            logger.info("worker_pipeline_skipped_missing_groq_key", user_id=user_id)
            return {
                "user_id": user_id,
                "new_emails_synced": 0,
                "emails_analyzed": 0,
                "drafts_created": 0,
                "status": "awaiting_groq_key",
            }

    if auth_svc and hasattr(auth_svc, "get_user_groq_api_key"):
        try:
            res_k = auth_svc.get_user_groq_api_key(user_id)
            if hasattr(res_k, "__await__"):
                val = await res_k
            else:
                val = res_k
            if isinstance(val, str) and val.strip():
                user_groq_key = val.strip()
        except Exception as exc:
            logger.warning("worker_get_user_groq_key_failed", user_id=user_id, error=str(exc))

    # 2. Process recent unanalyzed emails for this user (ensure all are digested)
    email_items = await email_repo.list_by_user(user_id=user_id, limit=50)
    analyzed_count = 0
    drafts_created = 0

    for item in email_items:
        existing_analysis = await email_repo.get_analysis(item.id)
        if not existing_analysis:
            full_email = await email_repo.get_by_id(item.id)
            if not full_email:
                continue

            # Perform Groq LLM analysis
            if user_groq_key:
                analysis_data = await groq_svc.analyze_email(full_email, api_key=user_groq_key)
            else:
                analysis_data = await groq_svc.analyze_email(full_email)
            stored_analysis = await email_repo.upsert_analysis(analysis_data)
            analyzed_count += 1

            # Check if reply draft should be generated
            if stored_analysis.needs_reply and not any(
                "phishing" in f or "malware" in f for f in stored_analysis.risk_flags
            ):
                try:
                    if hasattr(draft_repo, "get_by_email_id"):
                        res = draft_repo.get_by_email_id(full_email.id)
                        if hasattr(res, "__await__"):
                            existing_draft = await res
                            if existing_draft:
                                logger.info(
                                    "skipping_draft_generation_already_exists",
                                    email_id=str(full_email.id),
                                    draft_id=str(existing_draft.id),
                                )
                                continue

                    if user_groq_key:
                        draft = await approval_svc.generate_draft_for_email(
                            full_email.id, user_id, api_key=user_groq_key
                        )
                    else:
                        draft = await approval_svc.generate_draft_for_email(full_email.id, user_id)
                    versions = await draft_repo.get_versions(draft.id)
                    if versions:
                        await notif_svc.notify_new_draft(
                            draft=draft,
                            version=versions[-1],
                            email=full_email,
                            analysis=stored_analysis,
                        )
                        drafts_created += 1
                except Exception as exc:
                    logger.error(
                        "pipeline_draft_generation_failed",
                        email_id=str(full_email.id),
                        error=str(exc),
                    )

    return {
        "user_id": user_id,
        "new_emails_synced": new_count,
        "emails_analyzed": analyzed_count,
        "drafts_created": drafts_created,
    }


async def run_user_digest(ctx: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Execute daily digest compilation and dispatch for a user."""
    digest_svc: DigestService = ctx["digest_service"]
    return await digest_svc.generate_and_dispatch_digest(user_id)


async def scheduler_cron_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Periodic 5-minute scheduler.

    Claims due users, advances schedules, and enqueues deterministic jobs.
    """
    user_repo: UserRepo = ctx["user_repo"]
    redis = ctx.get("redis")

    # Claim due users with atomic FOR UPDATE SKIP LOCKED
    due_users = await user_repo.claim_due_users(limit=20)
    claimed_count = len(due_users)
    now = datetime.now(UTC)

    telegram_repo = ctx.get("telegram_repo")
    for user in due_users:
        if not (user.prefs and user.prefs.get("groq_api_key_encrypted")):
            logger.debug("worker_cron_skipped_missing_groq_key", user_id=str(user.id))
            continue

        if telegram_repo is not None:
            get_link_fn = getattr(telegram_repo, "get_link_by_user_id", None)
            if inspect.iscoroutinefunction(get_link_fn):
                tg_link = await get_link_fn(user.id)
                if not tg_link or not tg_link.is_active:
                    logger.debug("worker_sync_skipped_user_disconnected", user_id=str(user.id))
                    continue

        # 1. Calculate next wall-clock digest slot (9 AM / 3 PM / 9 PM) in user timezone
        user_tz = user.timezone if user.timezone and user.timezone != "UTC" else "Asia/Kolkata"
        next_digest = compute_next_digest_time(
            digest_time=user.digest_time,
            user_timezone=user_tz,
            now=now,
            digest_slots=DEFAULT_DIGEST_SLOTS,
        )
        await user_repo.update_user(
            user.id,
            UserUpdate(next_digest_at=next_digest),
        )

        # 2. Enqueue digest job with slot-specific deterministic job ID
        digest_job_id = f"digest_{user.id}_{now.strftime('%Y%m%d%H%M')}"
        if redis:
            await redis.enqueue_job(
                "run_user_digest",
                str(user.id),
                _job_id=digest_job_id,
            )

        # 3. Enqueue hourly sync job with deterministic job ID
        sync_job_id = f"sync_{user.id}_{now.strftime('%Y%m%d%H')}"
        if redis:
            await redis.enqueue_job(
                "run_user_sync_pipeline",
                str(user.id),
                _job_id=sync_job_id,
            )

    # Dispatch any due user-scheduled outbound emails
    try:
        await ScheduledEmailService(settings=ctx.get("settings")).dispatch_due_scheduled_emails()
    except Exception as exc:
        logger.error("worker_scheduled_emails_dispatch_failed", error=str(exc))

    logger.info("scheduler_cron_run_finished", claimed_users=claimed_count)
    return {"claimed_users": claimed_count}


# -----------------------------------------------------------------------------
# Worker Settings Class for CLI execution: `arq app.queue.worker.WorkerSettings`
# -----------------------------------------------------------------------------
class WorkerSettings:
    functions = [run_user_sync_pipeline, run_user_digest, scheduler_cron_job]
    cron_jobs = [cron(scheduler_cron_job, minute=set(range(0, 60, 5)))]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    on_startup = startup
    on_shutdown = shutdown
