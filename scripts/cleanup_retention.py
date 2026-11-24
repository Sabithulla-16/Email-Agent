"""CLI script to execute data retention cleanup.

Usage:
    uv run python scripts/cleanup_retention.py --days 30
"""

import argparse
import asyncio

import structlog

from app.core.config import get_settings
from app.db.client import close_supabase, init_supabase
from app.services.retention_service import RetentionService

logger = structlog.get_logger("scripts.retention")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Prune expired email bodies and tokens.")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Retention period in days for raw email bodies (default: 30)",
    )
    args = parser.parse_args()

    settings = get_settings()
    await init_supabase(settings)

    try:
        service = RetentionService()
        pruned_bodies = await service.prune_old_email_bodies(retention_days=args.days)
        pruned_tokens = await service.prune_expired_tokens_and_sessions()

        print("Data retention cleanup completed:")
        print(f"  • Email bodies pruned: {pruned_bodies}")
        print(f"  • Expired sessions pruned: {pruned_tokens.get('sessions', 0)}")
        print(f"  • Link tokens pruned: {pruned_tokens.get('link_tokens', 0)}")
        print(f"  • Callback tokens pruned: {pruned_tokens.get('callback_tokens', 0)}")
    finally:
        await close_supabase()


if __name__ == "__main__":
    asyncio.run(main())
