"""Script to clear emails, analyses, drafts, and sync states from Supabase."""

import asyncio

from app.core.config import get_settings
from app.db.client import close_supabase, get_supabase, init_supabase


async def main() -> None:
    settings = get_settings()
    await init_supabase(settings)
    client = get_supabase()

    print("Clearing data from Supabase...")
    tables = [
        "draft_versions",
        "drafts",
        "email_analysis",
        "emails",
        "sync_state",
        "callback_tokens",
        "pending_interactions",
    ]

    dummy_id = "00000000-0000-0000-0000-000000000000"
    for table in tables:
        try:
            res = await client.table(table).delete().neq("id", dummy_id).execute()
            count = len(res.data) if res.data else 0
            print(f"Cleared {count} rows from {table}")
        except Exception:
            try:
                res = await (
                    client.table(table).delete().neq("created_at", "1970-01-01T00:00:00Z").execute()
                )
                count = len(res.data) if res.data else 0
                print(f"Cleared {count} rows from {table}")
            except Exception as e2:
                print(f"Failed to clear {table}: {e2}")

    await close_supabase()
    print("Done clearing previous email/draft data!")


if __name__ == "__main__":
    asyncio.run(main())
