"""ARQ background worker for scheduled email sync, Groq analysis, drafting, and digests."""

from datetime import UTC, datetime
from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.db.client import close_supabase, get_supabase, init_supabase
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo
from app.models.db import UserUpdate
from app.services.approval_service import ApprovalService
from app.services.auth_service import AuthService
from app.services.digest_service import DigestService, compute_next_digest_time
from app.services.groq_service import GroqService
from app.services.notification_service import NotificationService
from app.services.sync_service import SyncService

logger = structlog.get_logger("app.queue.worker")


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize Supabase client, repositories, and domain services in ARQ context."""
    settings = get_settings()
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
    new_count = sync_res.get("new_emails_synced", 0)

    # 2. Process recent unanalyzed emails for this user
    email_items = await email_repo.list_by_user(user_id=user_id, limit=20)
    analyzed_count = 0
    drafts_created = 0

    for item in email_items:
        existing_analysis = await email_repo.get_analysis(item.id)
        if not existing_analysis:
            full_email = await email_repo.get_by_id(item.id)
            if not full_email:
                continue

            # Perform Groq LLM analysis
            analysis_data = await groq_svc.analyze_email(full_email)
            stored_analysis = await email_repo.upsert_analysis(analysis_data)
            analyzed_count += 1

            # Check if reply draft should be generated
            if stored_analysis.needs_reply and not any(
                "phishing" in f or "malware" in f for f in stored_analysis.risk_flags
            ):
                try:
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

    for user in due_users:
        # 1. Calculate next wall-clock digest time in user timezone
        next_digest = compute_next_digest_time(
            digest_time=user.digest_time,
            user_timezone=user.timezone,
            now=now,
        )
        await user_repo.update_user(
            user.id,
            UserUpdate(next_digest_at=next_digest),
        )

        # 2. Enqueue digest job with deterministic job ID
        digest_job_id = f"digest_{user.id}_{now.strftime('%Y%m%d')}"
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
