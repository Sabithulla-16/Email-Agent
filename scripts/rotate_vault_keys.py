"""Script to re-encrypt stored OAuth refresh tokens with the latest encryption key version."""

import asyncio
from typing import Any, cast

import structlog

from app.core.config import get_settings
from app.core.vault import get_token_vault
from app.db.client import close_supabase, init_supabase

logger = structlog.get_logger("scripts.rotate_vault_keys")


async def rotate_tokens() -> None:
    settings = get_settings()
    vault = get_token_vault()
    current_version = settings.TOKEN_KEY_VERSION

    client = await init_supabase(settings)
    logger.info("starting_token_rotation", target_version=current_version)

    # Fetch all tokens where key_version is outdated
    res = await (
        client.table("oauth_tokens")
        .select("user_id, refresh_token_enc, key_version")
        .neq("key_version", current_version)
        .execute()
    )

    rows = cast(list[dict[str, Any]], res.data)
    logger.info("outdated_tokens_found", count=len(rows))

    updated_count = 0
    for row in rows:
        user_id = row["user_id"]
        old_ciphertext = row["refresh_token_enc"]
        old_ver = row["key_version"]

        try:
            # Decrypt with old key version
            plaintext = vault.decrypt(old_ciphertext, old_ver)
            # Re-encrypt with active version
            new_ciphertext, new_ver = vault.encrypt(plaintext, current_version)

            await (
                client.table("oauth_tokens")
                .update({
                    "refresh_token_enc": new_ciphertext,
                    "key_version": new_ver,
                })
                .eq("user_id", user_id)
                .execute()
            )
            updated_count += 1
            logger.info(
                "token_rotated_for_user",
                user_id=user_id,
                from_version=old_ver,
                to_version=new_ver,
            )
        except Exception as exc:
            logger.error("token_rotation_failed_for_user", user_id=user_id, error=str(exc))

    logger.info("rotation_complete", updated_count=updated_count, total=len(rows))
    await close_supabase()


if __name__ == "__main__":
    asyncio.run(rotate_tokens())
